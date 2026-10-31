"""
INNOVATRIX Backend — AI-Based Rockfall Prediction and Alert System
Real-time sensor ingestion + risk engine + WebSocket broadcast + dashboard serving.

Run:  pip install -r requirements.txt
      python main.py
Then open http://<this-machine-ip>:8000 in a browser (dashboard),
and point your ESP32 nodes at http://<this-machine-ip>:8000/api/sensor-data
"""

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    import requests  # only needed for NodeODM photogrammetry integration
except ImportError:
    requests = None

import notify   # real-time alert dispatch (Telegram / email / SMS / webhook)
import weather  # live weather + forecast-aware risk (Open-Meteo, free)

DEVICE_TIMEOUT_SEC = 15  # no data for this long -> mark node/camera as disconnected

app = FastAPI(title="INNOVATRIX - Rockfall Prediction System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# ZONE CONFIGURATION
# Each zone = one physical monitoring area of the pit mine slope.
# base_risk = static geological factor (slope angle / rock type) baked in
# manually per zone — in a full production system this comes from DEM +
# slope-stability analysis (e.g. SSR / limit equilibrium) run offline.
# ---------------------------------------------------------------------------
ZONES: Dict[str, dict] = {
    "A": {"name": "Zone A - North Bench", "base_risk": 8,  "angle": 0},
    "B": {"name": "Zone B - North-East Face", "base_risk": 15, "angle": 60},
    "C": {"name": "Zone C - East Bench", "base_risk": 5,  "angle": 120},
    "D": {"name": "Zone D - South Bench", "base_risk": 10, "angle": 180},
    "E": {"name": "Zone E - South-West Face", "base_risk": 18, "angle": 240},
    "F": {"name": "Zone F - West Bench", "base_risk": 6,  "angle": 300},
}

RISK_THRESHOLD_HIGH = 70
RISK_THRESHOLD_MED = 40
VIBRATION_WINDOW = 10  # readings kept for rolling RMS

# live state per zone
zone_state: Dict[str, dict] = {
    zid: {
        **z,
        "id": zid,
        "vibration": 0.0,
        "vibration_rms": 0.0,
        "tilt": 0.0,
        "rainfall": 0.0,
        "temperature": 25.0,
        "humidity": 50.0,
        "risk_score": z["base_risk"],
        "risk_level": "Low",
        "drone_requested": False,
        "workers": 0,             # live crew headcount in this zone (check-in/out)
        "last_updated": None,
        "weather_source": None,   # "sensor" | "api" | None
        "forecast_boost": 0.0,    # risk points added by incoming-rain forecast
        # --- device connection state ---
        "sensor_connected": False,
        "sensor_ip": None,
        "sensor_last_seen": None,
        "camera_connected": False,
        "camera_url": None,
        "camera_last_seen": None,
        "_vib_window": deque(maxlen=VIBRATION_WINDOW),
    }
    for zid, z in ZONES.items()
}

alerts: List[dict] = []
connected_clients: List[WebSocket] = []

# ---------------------------------------------------------------------------
# WORKFORCE TRACKING — headcount per zone, survives restarts via workers.json
# ---------------------------------------------------------------------------
WORKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workers.json")


def _load_workers():
    try:
        with open(WORKERS_FILE) as f:
            data = json.load(f)
        for zid, n in data.items():
            if zid in zone_state:
                zone_state[zid]["workers"] = max(0, int(n))
    except Exception:
        pass  # no file yet / unreadable — start at zero


def _save_workers():
    try:
        with open(WORKERS_FILE, "w") as f:
            json.dump({zid: z["workers"] for zid, z in zone_state.items()}, f)
    except Exception:
        pass


_load_workers()


class SensorPayload(BaseModel):
    zone_id: str
    vibration: float = 0.0     # m/s^2 magnitude from accelerometer, gravity-removed
    tilt: float = 0.0          # degrees
    rainfall: float = 0.0      # mm (cumulative or rate, sensor-dependent)
    temperature: float = 25.0  # C
    humidity: float = 50.0     # %


def compute_risk(zone: dict) -> tuple[float, str]:
    """
    Weighted fusion risk model.
    vibration 40%, tilt 25%, rainfall 20%, base geological risk 15%.
    Each normalized 0-1 against a practical field ceiling, then weighted.
    Swap this function for a trained ML model (XGBoost/CNN-LSTM) later —
    the interface (zone dict in, score+level out) stays the same.
    """
    vib_norm = min(zone["vibration_rms"] / 2.0, 1.0)
    tilt_norm = min(zone["tilt"] / 30.0, 1.0)
    rain_norm = min(zone["rainfall"] / 50.0, 1.0)
    base_norm = zone["base_risk"] / 100.0

    score = (vib_norm * 40) + (tilt_norm * 25) + (rain_norm * 20) + (base_norm * 15)
    # forecast-aware: heavy rain expected in next 6h pre-emptively raises risk
    boost = weather.forecast_boost()
    zone["forecast_boost"] = boost
    score += boost
    score = round(min(max(score, 0), 100), 1)

    if score >= RISK_THRESHOLD_HIGH:
        level = "Critical" if score >= 85 else "High"
    elif score >= RISK_THRESHOLD_MED:
        level = "Moderate"
    else:
        level = "Low"
    return score, level


async def broadcast(message: dict):
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(ws)
    for d in dead:
        if d in connected_clients:
            connected_clients.remove(d)


def raise_alert(zone: dict, reason: str):
    # people-exposure context: a hot zone with crew inside is a different emergency
    n_workers = zone.get("workers", 0)
    if n_workers > 0 and zone["risk_level"] in ("High", "Critical"):
        reason = f"{reason} — ⚠ {n_workers} worker(s) in this zone"
    alert = {
        "id": str(uuid.uuid4())[:8],
        "zone_id": zone["id"],
        "zone_name": zone["name"],
        "level": zone["risk_level"],
        "score": zone["risk_score"],
        "workers": n_workers,
        "reason": reason,
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    alerts.insert(0, alert)
    del alerts[200:]  # cap history
    return alert


@app.post("/api/sensor-data")
async def ingest_sensor_data(payload: SensorPayload, request: Request):
    """ESP32 nodes POST here every few seconds."""
    zid = payload.zone_id.upper()
    if zid not in zone_state:
        return {"error": f"unknown zone_id '{zid}'", "valid_zones": list(ZONES.keys())}

    zone = zone_state[zid]

    was_connected = zone["sensor_connected"]
    zone["sensor_connected"] = True
    zone["sensor_ip"] = request.client.host if request.client else None
    zone["sensor_last_seen"] = datetime.now().isoformat()
    if not was_connected:
        raise_alert(zone, f"sensor node connected from {zone['sensor_ip']}")

    zone["_vib_window"].append(payload.vibration)
    rms = (sum(v * v for v in zone["_vib_window"]) / len(zone["_vib_window"])) ** 0.5

    zone["weather_source"] = "sensor"  # live hardware overrides API weather
    zone["vibration"] = round(payload.vibration, 3)
    zone["vibration_rms"] = round(rms, 3)
    zone["tilt"] = round(payload.tilt, 2)
    zone["rainfall"] = round(payload.rainfall, 2)
    zone["temperature"] = round(payload.temperature, 1)
    zone["humidity"] = round(payload.humidity, 1)
    zone["last_updated"] = datetime.now().isoformat()

    prev_level = zone["risk_level"]
    score, level = compute_risk(zone)
    zone["risk_score"] = score
    zone["risk_level"] = level

    new_alert = None
    # Alert on threshold crossing into Moderate/High/Critical (not on every packet)
    if level in ("Moderate", "High", "Critical") and level != prev_level:
        reasons = []
        if zone["vibration_rms"] / 2.0 > 0.5:
            reasons.append("vibration spike")
        if zone["tilt"] / 30.0 > 0.5:
            reasons.append("tilt deviation")
        if zone["rainfall"] / 50.0 > 0.5:
            reasons.append("heavy rainfall")
        reason = " + ".join(reasons) if reasons else "risk threshold crossed"
        new_alert = raise_alert(zone, reason)
        # real-world dispatch: Telegram / email / SMS / webhook subscribers
        notify.dispatch(new_alert, prev_level=prev_level)

    # Drone auto-request when High/Critical and not already flagged
    if level in ("High", "Critical") and not zone["drone_requested"]:
        zone["drone_requested"] = True
        raise_alert(zone, "drone survey auto-requested for visual confirmation")
    elif level in ("Low", "Moderate"):
        zone["drone_requested"] = False

    out_zone = {k: v for k, v in zone.items() if not k.startswith("_")}
    await broadcast({
        "type": "zone_update",
        "zone": out_zone,
        "alert": new_alert,
    })
    return {"status": "ok", "risk_score": score, "risk_level": level}


@app.get("/api/zones")
async def get_zones():
    return [{k: v for k, v in z.items() if not k.startswith("_")} for z in zone_state.values()]


@app.get("/api/alerts")
async def get_alerts():
    return alerts[:50]


@app.post("/api/drone/{zone_id}/complete")
async def drone_complete(zone_id: str, crack_detected: bool = False):
    """Call this once a drone survey for a zone finishes (manual or automated CV pipeline)."""
    zid = zone_id.upper()
    if zid not in zone_state:
        return {"error": "unknown zone"}
    zone = zone_state[zid]
    zone["drone_requested"] = False
    if crack_detected:
        zone["base_risk"] = min(zone["base_risk"] + 20, 100)
        alert = raise_alert(zone, "drone imagery confirmed crack/instability")
        await broadcast({"type": "zone_update", "zone": {k: v for k, v in zone.items() if not k.startswith("_")}, "alert": alert})
    return {"status": "ok"}


class WorkersPayload(BaseModel):
    count: Optional[int] = None   # absolute headcount for the zone
    delta: Optional[int] = None   # +/- adjustment (crew check-in / check-out)


@app.post("/api/workers/{zone_id}")
async def set_workers(zone_id: str, payload: WorkersPayload):
    """Crew check-in/out: live headcount of people working in each zone."""
    zid = zone_id.upper()
    if zid not in zone_state:
        return {"error": "unknown zone"}
    zone = zone_state[zid]
    n = zone.get("workers", 0)
    if payload.count is not None:
        n = payload.count
    if payload.delta is not None:
        n += payload.delta
    zone["workers"] = max(0, min(int(n), 999))
    _save_workers()
    await broadcast({
        "type": "zone_update",
        "zone": {k: v for k, v in zone.items() if not k.startswith("_")},
        "alert": None,
    })
    return {"status": "ok", "zone_id": zid, "workers": zone["workers"]}


class CameraConnectPayload(BaseModel):
    zone_id: str
    stream_url: str  # e.g. http://<esp32-cam-ip>/stream (MJPEG) — set once the camera module is added


@app.post("/api/camera/connect")
async def connect_camera(payload: CameraConnectPayload):
    """Register a camera/drone video feed URL for a zone. Call this once the
    ESP32-CAM (or drone downlink) is physically wired and on the network —
    no code change needed, judges can do this live from the dashboard."""
    zid = payload.zone_id.upper()
    if zid not in zone_state:
        return {"error": f"unknown zone_id '{zid}'", "valid_zones": list(ZONES.keys())}
    zone = zone_state[zid]
    zone["camera_url"] = payload.stream_url
    zone["camera_connected"] = True
    zone["camera_last_seen"] = datetime.now().isoformat()
    alert = raise_alert(zone, f"camera connected: {payload.stream_url}")
    await broadcast({"type": "zone_update", "zone": {k: v for k, v in zone.items() if not k.startswith("_")}, "alert": alert})
    return {"status": "ok"}


@app.post("/api/camera/{zone_id}/heartbeat")
async def camera_heartbeat(zone_id: str):
    """Camera/drone module pings this periodically to prove it's still alive
    (separately from posting actual images/detections)."""
    zid = zone_id.upper()
    if zid not in zone_state:
        return {"error": "unknown zone"}
    zone_state[zid]["camera_connected"] = True
    zone_state[zid]["camera_last_seen"] = datetime.now().isoformat()
    return {"status": "ok"}


@app.post("/api/camera/{zone_id}/disconnect")
async def disconnect_camera(zone_id: str):
    zid = zone_id.upper()
    if zid not in zone_state:
        return {"error": "unknown zone"}
    zone = zone_state[zid]
    zone["camera_connected"] = False
    zone["camera_url"] = None
    await broadcast({"type": "zone_update", "zone": {k: v for k, v in zone.items() if not k.startswith("_")}, "alert": None})
    return {"status": "ok"}


@app.get("/api/devices")
async def get_devices():
    """Connection status for every zone's sensor node + camera — powers the
    'Connect Device' panel on the dashboard."""
    out = []
    for zid, zone in zone_state.items():
        out.append({
            "zone_id": zid,
            "zone_name": zone["name"],
            "sensor_connected": zone["sensor_connected"],
            "sensor_ip": zone["sensor_ip"],
            "sensor_last_seen": zone["sensor_last_seen"],
            "camera_connected": zone["camera_connected"],
            "camera_url": zone["camera_url"],
            "camera_last_seen": zone["camera_last_seen"],
        })
    return out


# ---------------------------------------------------------------------------
# DRONE SURVEY -> 3D RECONSTRUCTION (photogrammetry)
#
# Flow: upload drone images per zone -> either
#   (a) one-click process through NodeODM (OpenDroneMap's API server —
#       run it with:  docker run -ti -p 3001:3000 opendronemap/nodeodm )
#       -> textured 3D model auto-downloads into surveys/<zone>/model/
#   (b) or process in WebODM yourself and upload the finished model file
#       (.ply / .obj+.mtl+textures / .glb) directly.
# The dashboard's Survey Viewer renders whatever model a zone has.
# A synthetic sample reconstruction ships in surveys/sample/ so the viewer
# can be demoed before any real drone flight.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SURVEYS_DIR = os.path.join(BASE_DIR, "surveys")
os.makedirs(SURVEYS_DIR, exist_ok=True)

MODEL_EXTS = (".ply", ".obj", ".glb", ".gltf")
_main_loop: Optional[asyncio.AbstractEventLoop] = None

surveys: Dict[str, dict] = {}  # zone_id -> {status, images, model_url, message, updated}


def _survey_default(zid: str) -> dict:
    return {"zone_id": zid, "status": "none", "images": 0, "model_url": None,
            "message": None, "updated": None}


def _zone_dir(zid: str, sub: str = "") -> str:
    p = os.path.join(SURVEYS_DIR, zid, sub) if sub else os.path.join(SURVEYS_DIR, zid)
    os.makedirs(p, exist_ok=True)
    return p


def _find_model_file(model_dir: str) -> Optional[str]:
    """Pick the main renderable model file (prefer glb > obj > ply)."""
    if not os.path.isdir(model_dir):
        return None
    found = {}
    for root, _dirs, files in os.walk(model_dir):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in MODEL_EXTS:
                found.setdefault(ext, os.path.join(root, fn))
    for ext in (".glb", ".gltf", ".obj", ".ply"):
        if ext in found:
            return os.path.relpath(found[ext], SURVEYS_DIR).replace(os.sep, "/")
    return None


def _scan_surveys():
    for zid in ZONES:
        s = _survey_default(zid)
        img_dir = os.path.join(SURVEYS_DIR, zid, "images")
        if os.path.isdir(img_dir):
            s["images"] = len([f for f in os.listdir(img_dir)
                               if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff")])
            if s["images"]:
                s["status"] = "images_uploaded"
        rel = _find_model_file(os.path.join(SURVEYS_DIR, zid, "model"))
        if rel:
            s["status"] = "complete"
            s["model_url"] = f"/surveys/{rel}"
        surveys[zid] = s


_scan_surveys()


def _broadcast_survey(zid: str):
    """Thread-safe broadcast of a survey update to all dashboards."""
    msg = {"type": "survey_update", "survey": surveys[zid]}
    if _main_loop is not None:
        asyncio.run_coroutine_threadsafe(broadcast(msg), _main_loop)


def _set_survey(zid: str, **kw):
    surveys[zid].update(kw, updated=datetime.now().isoformat())
    _broadcast_survey(zid)


@app.get("/api/odm/status")
async def odm_status(url: str = "http://localhost:3001"):
    """Ping a NodeODM engine so the UI can show online/offline before processing."""
    if requests is None:
        return {"online": False, "detail": "python 'requests' not installed"}
    def _ping():
        try:
            r = requests.get(url.rstrip("/") + "/info", timeout=3)
            if r.ok:
                j = r.json()
                return {"online": True, "version": j.get("version", "?"),
                        "queue": j.get("taskQueueCount", 0)}
        except Exception as e:
            return {"online": False, "detail": str(e)[:120]}
        return {"online": False, "detail": "bad response"}
    return await asyncio.get_running_loop().run_in_executor(None, _ping)


@app.get("/api/surveys")
async def get_surveys():
    sample = os.path.join(SURVEYS_DIR, "sample")
    sample_rel = _find_model_file(sample)
    return {
        "surveys": list(surveys.values()),
        "sample_model_url": f"/surveys/{sample_rel}" if sample_rel else None,
    }


@app.post("/api/survey/{zone_id}/images")
async def upload_survey_images(zone_id: str, files: List[UploadFile] = File(...)):
    zid = zone_id.upper()
    if zid not in surveys:
        return {"error": "unknown zone"}
    img_dir = _zone_dir(zid, "images")
    saved = 0
    for uf in files:
        ext = os.path.splitext(uf.filename or "")[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
            continue
        # phone cameras reuse the same filename for every capture — never overwrite
        base, dest = os.path.splitext(os.path.basename(uf.filename))[0], None
        dest = os.path.join(img_dir, f"{base}{ext}")
        while os.path.exists(dest):
            dest = os.path.join(img_dir, f"{base}_{uuid.uuid4().hex[:6]}{ext}")
        with open(dest, "wb") as out:
            shutil.copyfileobj(uf.file, out)
        saved += 1
    total = len(os.listdir(img_dir))
    _set_survey(zid, images=total,
                status="images_uploaded" if surveys[zid]["status"] in ("none", "images_uploaded", "failed") else surveys[zid]["status"],
                message=f"{saved} image(s) added")
    return {"status": "ok", "saved": saved, "total": total}


@app.post("/api/survey/{zone_id}/model")
async def upload_survey_model(zone_id: str, files: List[UploadFile] = File(...)):
    """Directly upload an already-reconstructed model (from WebODM etc.):
    a .ply / .glb, or an .obj together with its .mtl + texture files."""
    zid = zone_id.upper()
    if zid not in surveys:
        return {"error": "unknown zone"}
    model_dir = os.path.join(SURVEYS_DIR, zid, "model")
    if os.path.isdir(model_dir):
        shutil.rmtree(model_dir)
    os.makedirs(model_dir, exist_ok=True)
    for uf in files:
        dest = os.path.join(model_dir, os.path.basename(uf.filename or "file"))
        with open(dest, "wb") as out:
            shutil.copyfileobj(uf.file, out)
    rel = _find_model_file(model_dir)
    if not rel:
        return {"error": f"no renderable model found — upload one of {MODEL_EXTS}"}
    _set_survey(zid, status="complete", model_url=f"/surveys/{rel}", message="3D model uploaded")
    raise_alert(zone_state[zid], "drone survey 3D model added to dashboard")
    return {"status": "ok", "model_url": f"/surveys/{rel}"}


class ProcessPayload(BaseModel):
    odm_url: str = "http://localhost:3001"


def _odm_worker(zid: str, odm_url: str):
    """Blocking worker thread: submit images to NodeODM, poll, download model."""
    try:
        img_dir = os.path.join(SURVEYS_DIR, zid, "images")
        imgs = [os.path.join(img_dir, f) for f in sorted(os.listdir(img_dir))]
        files = [("images", (os.path.basename(p), open(p, "rb"), "image/jpeg")) for p in imgs]
        opts = json.dumps([{"name": "pc-quality", "value": "medium"},
                           {"name": "mesh-octree-depth", "value": "10"}])
        r = requests.post(f"{odm_url}/task/new", files=files,
                          data={"options": opts, "name": f"innovatrix-{zid}"}, timeout=600)
        for _, (_, fh, _) in files:
            fh.close()
        r.raise_for_status()
        task_uuid = r.json()["uuid"]
        _set_survey(zid, message=f"NodeODM task {task_uuid[:8]} running…")
        while True:
            time.sleep(8)
            info = requests.get(f"{odm_url}/task/{task_uuid}/info", timeout=30).json()
            code = info.get("status", {}).get("code")
            if code == 40:  # COMPLETED
                break
            if code in (30, 50):  # FAILED / CANCELED
                _set_survey(zid, status="failed", message=f"NodeODM: {info.get('status', {}).get('errorMessage', 'failed')}")
                return
            prog = info.get("progress", 0)
            _set_survey(zid, message=f"processing… {prog:.0f}%")
        # download + extract the textured model
        _set_survey(zid, message="downloading reconstruction…")
        zpath = os.path.join(_zone_dir(zid), "odm_result.zip")
        with requests.get(f"{odm_url}/task/{task_uuid}/download/all.zip", stream=True, timeout=1200) as resp:
            resp.raise_for_status()
            with open(zpath, "wb") as out:
                for chunk in resp.iter_content(1024 * 512):
                    out.write(chunk)
        model_dir = os.path.join(SURVEYS_DIR, zid, "model")
        if os.path.isdir(model_dir):
            shutil.rmtree(model_dir)
        os.makedirs(model_dir, exist_ok=True)
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                low = name.lower()
                if ("odm_texturing" in low or "odm_meshing" in low) and not name.endswith("/"):
                    target = os.path.join(model_dir, os.path.basename(name))
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        os.remove(zpath)
        rel = _find_model_file(model_dir)
        if rel:
            _set_survey(zid, status="complete", model_url=f"/surveys/{rel}",
                        message="reconstruction complete")
            raise_alert(zone_state[zid], "drone survey 3D reconstruction ready")
        else:
            _set_survey(zid, status="failed", message="ODM finished but no model file found in output")
    except Exception as e:  # network down, ODM missing, etc.
        _set_survey(zid, status="failed", message=f"ODM error: {e.__class__.__name__}: {e}")


@app.post("/api/survey/{zone_id}/process")
async def process_survey(zone_id: str, payload: ProcessPayload):
    zid = zone_id.upper()
    if zid not in surveys:
        return {"error": "unknown zone"}
    if requests is None:
        return {"error": "python 'requests' package missing — pip install requests"}
    if surveys[zid]["images"] < 3:
        return {"error": "upload at least 3 overlapping drone images first"}
    odm = payload.odm_url.rstrip("/")
    try:
        ping = requests.get(f"{odm}/info", timeout=5)
        ping.raise_for_status()
    except Exception:
        return {"error": f"NodeODM not reachable at {odm}. Start it with: "
                         f"docker run -ti -p 3001:3000 opendronemap/nodeodm"}
    _set_survey(zid, status="processing", message="submitting images to NodeODM…")
    threading.Thread(target=_odm_worker, args=(zid, odm), daemon=True).start()
    return {"status": "ok"}


@app.delete("/api/survey/{zone_id}")
async def delete_survey(zone_id: str):
    zid = zone_id.upper()
    if zid not in surveys:
        return {"error": "unknown zone"}
    zdir = os.path.join(SURVEYS_DIR, zid)
    if os.path.isdir(zdir):
        shutil.rmtree(zdir)
    surveys[zid] = _survey_default(zid)
    _set_survey(zid)
    return {"status": "ok"}


async def device_timeout_watcher():
    """Background loop: flips a device to 'disconnected' if it's gone quiet
    for DEVICE_TIMEOUT_SEC, and pushes the change to the dashboard live."""
    while True:
        await asyncio.sleep(5)
        now = datetime.now()
        for zid, zone in zone_state.items():
            changed = False
            for kind, ts_key, conn_key in (
                ("sensor", "sensor_last_seen", "sensor_connected"),
                ("camera", "camera_last_seen", "camera_connected"),
            ):
                ts = zone[ts_key]
                if ts and zone[conn_key]:
                    age = (now - datetime.fromisoformat(ts)).total_seconds()
                    if age > DEVICE_TIMEOUT_SEC:
                        zone[conn_key] = False
                        changed = True
            if changed:
                await broadcast({
                    "type": "zone_update",
                    "zone": {k: v for k, v in zone.items() if not k.startswith("_")},
                    "alert": None,
                })


@app.on_event("startup")
async def start_background_tasks():
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    asyncio.create_task(device_timeout_watcher())
    # thread-safe broadcast bridge for the notify worker threads
    notify.init(lambda msg: _main_loop is not None
                and asyncio.run_coroutine_threadsafe(broadcast(msg), _main_loop))
    weather.load_config()
    asyncio.create_task(weather_poller())


# ---------------------------------------------------------------------------
# WEATHER — live conditions + forecast-aware risk (Open-Meteo)
# ---------------------------------------------------------------------------

def _reevaluate_zone_after_weather(zone: dict):
    """Recompute a zone's risk after weather changed; fire alerts like ingest does."""
    prev_level = zone["risk_level"]
    score, level = compute_risk(zone)
    zone["risk_score"] = score
    zone["risk_level"] = level
    new_alert = None
    if level in ("Moderate", "High", "Critical") and level != prev_level:
        reasons = []
        if zone["vibration_rms"] / 2.0 > 0.5:
            reasons.append("vibration spike")
        if zone["tilt"] / 30.0 > 0.5:
            reasons.append("tilt deviation")
        if zone["rainfall"] / 50.0 > 0.5:
            reasons.append("heavy rainfall")
        if zone.get("forecast_boost", 0) >= 5:
            reasons.append("heavy rain forecast")
        reason = " + ".join(reasons) if reasons else "risk threshold crossed"
        new_alert = raise_alert(zone, reason)
        notify.dispatch(new_alert, prev_level=prev_level)
    if level in ("High", "Critical") and not zone["drone_requested"]:
        zone["drone_requested"] = True
        raise_alert(zone, "drone survey auto-requested for visual confirmation")
    elif level in ("Low", "Moderate"):
        zone["drone_requested"] = False
    return new_alert


async def weather_poller():
    """Fetch weather on a schedule; fill sensor-less zones; re-run risk for all."""
    while True:
        if weather.get_config().get("enabled"):
            await asyncio.get_running_loop().run_in_executor(None, weather.refresh)
            api_vals = weather.api_zone_values()
            for zone in zone_state.values():
                if api_vals is not None and not zone["sensor_connected"]:
                    zone["rainfall"] = api_vals["rainfall"]
                    zone["temperature"] = api_vals["temperature"]
                    zone["humidity"] = api_vals["humidity"]
                    zone["weather_source"] = "api"
                    zone["last_updated"] = datetime.now().isoformat()
                alert = _reevaluate_zone_after_weather(zone)
                await broadcast({
                    "type": "zone_update",
                    "zone": {k: v for k, v in zone.items() if not k.startswith("_")},
                    "alert": alert,
                })
            await broadcast({"type": "weather_update",
                             "weather": weather.state, "config": weather.get_config()})
        await asyncio.sleep(weather.poll_seconds())


class WeatherConfigPayload(BaseModel):
    enabled: Optional[bool] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    place: Optional[str] = None
    poll_sec: Optional[int] = None


@app.get("/api/weather")
async def weather_get():
    return {"config": weather.get_config(), "weather": weather.state}


@app.post("/api/weather/config")
async def weather_set_config(payload: WeatherConfigPayload):
    patch = {k: v for k, v in payload.dict().items() if v is not None}
    cfg = weather.update_config(patch)
    if cfg.get("enabled") and cfg.get("lat") is not None:
        # immediate fetch + apply so the UI updates right away
        await asyncio.get_running_loop().run_in_executor(None, weather.refresh)
        api_vals = weather.api_zone_values()
        for zone in zone_state.values():
            if api_vals is not None and not zone["sensor_connected"]:
                zone["rainfall"] = api_vals["rainfall"]
                zone["temperature"] = api_vals["temperature"]
                zone["humidity"] = api_vals["humidity"]
                zone["weather_source"] = "api"
                zone["last_updated"] = datetime.now().isoformat()
            alert = _reevaluate_zone_after_weather(zone)
            await broadcast({
                "type": "zone_update",
                "zone": {k: v for k, v in zone.items() if not k.startswith("_")},
                "alert": alert,
            })
        await broadcast({"type": "weather_update",
                         "weather": weather.state, "config": cfg})
    return {"config": cfg, "weather": weather.state}


# ---------------------------------------------------------------------------
# NOTIFICATION / MESSAGE ALERT API
# ---------------------------------------------------------------------------

class SubscriberPayload(BaseModel):
    type: str            # telegram | email | sms | webhook
    target: str          # chat_id | email address | +91xxxxxxxxxx | URL
    name: str = ""
    min_level: str = "High"
    zones: List[str] = ["*"]


@app.get("/api/notify/config")
async def notify_get_config():
    return notify.get_config_masked()


@app.post("/api/notify/config")
async def notify_set_config(patch: dict):
    return notify.update_config(patch)


@app.post("/api/notify/subscribers")
async def notify_add_subscriber(payload: SubscriberPayload):
    try:
        sub = notify.add_subscriber(payload.dict())
        return {"status": "ok", "subscriber": sub}
    except ValueError as e:
        return {"status": "error", "error": str(e)}


@app.delete("/api/notify/subscribers/{sub_id}")
async def notify_remove_subscriber(sub_id: str):
    removed = notify.remove_subscriber(sub_id)
    return {"status": "ok" if removed else "not_found"}


@app.post("/api/notify/test")
async def notify_test(level: str = "Critical"):
    return notify.send_test(level)


@app.get("/api/notify/log")
async def notify_log():
    return list(notify.dispatch_log)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    # send initial snapshot
    await websocket.send_text(json.dumps({
        "type": "snapshot",
        "zones": [{k: v for k, v in z.items() if not k.startswith("_")} for z in zone_state.values()],
        "alerts": alerts[:50],
    }))
    try:
        while True:
            await websocket.receive_text()  # keep-alive / ignore client pings
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


# Serve survey files (drone images + reconstructed 3D models), then the
# dashboard. Resolved relative to this file's own location so it works no
# matter what directory you launch `python main.py` from.
app.mount("/surveys", StaticFiles(directory=SURVEYS_DIR), name="surveys")
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
