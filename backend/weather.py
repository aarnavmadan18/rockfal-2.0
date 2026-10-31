"""
INNOVATRIX — live weather intelligence (Open-Meteo, free, no API key).

What it does:
  - Polls current conditions + 48h hourly forecast for the mine's lat/lon
  - Zones with NO physical sensor get rainfall/temp/humidity from the API
    (clearly marked weather_source="api" — nothing silently faked; a live
    ESP32 always overrides the API for its zone)
  - Forecast-aware risk: heavy rain in the NEXT 6h adds a pre-emptive
    boost (up to +10) to every zone's risk — rockfalls follow rain, so
    knowing it's coming beats measuring it falling.

Config persists in backend/weather_config.json.
"""

import json
import os
import threading
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_config.json")
API_BASE = os.environ.get("OPEN_METEO_BASE", "https://api.open-meteo.com")

DEFAULT_CONFIG = {
    "enabled": False,
    "lat": None,
    "lon": None,
    "place": "",          # free-text label shown in the UI
    "poll_sec": 600,       # re-fetch every 10 min
}

_lock = threading.Lock()
_config: dict = {}

# latest fetched weather (None until first successful fetch)
state: dict = {
    "ok": False,
    "error": None,
    "fetched_at": None,
    "temperature": None,       # °C
    "humidity": None,          # %
    "rain_now": None,          # mm in current hour
    "rain_next_6h": None,      # mm total
    "rain_next_24h": None,     # mm total
    "rain_incoming": False,    # heavy rain expected within 6h
    "forecast_boost": 0.0,     # 0..10 risk points added by forecast
    "hourly": [],              # next 24 [{time, precip}] for the UI chart
}

# rainfall unit mapping: the risk engine normalizes rainfall against a 0-50
# scale (matching the analog sensor). 10 mm/h = violent rain -> map mm/h * 5,
# capped at 50, so API values line up with the sensor scale.
MM_PER_HOUR_TO_SCALE = 5.0
RAIN_INCOMING_MM_6H = 10.0     # >=10 mm expected in 6h counts as "incoming"
BOOST_FULL_AT_MM_6H = 30.0     # 30+ mm in 6h -> full +10 boost


def load_config():
    global _config
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[weather] bad weather_config.json ({e}) — using defaults")
    _config = cfg
    return _config


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_config, f, indent=2)


def get_config() -> dict:
    return dict(_config)


def update_config(patch: dict) -> dict:
    for k in ("enabled", "lat", "lon", "place", "poll_sec"):
        if k in patch:
            _config[k] = patch[k]
    save_config()
    return get_config()


def refresh() -> dict:
    """Blocking fetch (call from a worker thread). Updates `state`."""
    if not _config.get("enabled") or _config.get("lat") is None or _config.get("lon") is None:
        state.update(ok=False, error="weather disabled or location not set")
        return state
    if requests is None:
        state.update(ok=False, error="python 'requests' not installed")
        return state
    try:
        r = requests.get(
            f"{API_BASE}/v1/forecast",
            params={
                "latitude": _config["lat"],
                "longitude": _config["lon"],
                "current": "temperature_2m,relative_humidity_2m,precipitation",
                "hourly": "precipitation",
                "forecast_days": 2,
                "timezone": "auto",
            },
            timeout=12,
        )
        r.raise_for_status()
        j = r.json()
        cur = j.get("current", {})
        hourly = j.get("hourly", {})
        times = hourly.get("time", [])
        precip = hourly.get("precipitation", []) or []

        # find "now" index in the hourly series
        now_iso = cur.get("time", "")
        try:
            idx = times.index(now_iso[: len(times[0])]) if times else 0
        except ValueError:
            idx = 0

        nxt6 = [p or 0 for p in precip[idx: idx + 6]]
        nxt24 = [p or 0 for p in precip[idx: idx + 24]]
        rain6, rain24 = round(sum(nxt6), 1), round(sum(nxt24), 1)

        with _lock:
            state.update(
                ok=True,
                error=None,
                fetched_at=datetime.now().strftime("%H:%M:%S"),
                temperature=cur.get("temperature_2m"),
                humidity=cur.get("relative_humidity_2m"),
                rain_now=cur.get("precipitation", 0.0),
                rain_next_6h=rain6,
                rain_next_24h=rain24,
                rain_incoming=rain6 >= RAIN_INCOMING_MM_6H,
                forecast_boost=round(min(rain6 / BOOST_FULL_AT_MM_6H, 1.0) * 10.0, 1),
                hourly=[{"time": t, "precip": p or 0}
                        for t, p in list(zip(times, precip))[idx: idx + 24]],
            )
        print(f"[weather] {_config.get('place') or (_config['lat'], _config['lon'])}: "
              f"{state['temperature']}°C, rain now {state['rain_now']}mm, "
              f"6h {rain6}mm, 24h {rain24}mm, boost +{state['forecast_boost']}")
    except Exception as e:
        state.update(ok=False, error=str(e)[:200])
        print(f"[weather] fetch failed: {e}")
    return state


def api_zone_values() -> dict | None:
    """Sensor-equivalent values for zones with no hardware, or None if no data."""
    if not state["ok"]:
        return None
    return {
        "rainfall": round(min((state["rain_now"] or 0) * MM_PER_HOUR_TO_SCALE, 50.0), 2),
        "temperature": state["temperature"] if state["temperature"] is not None else 25.0,
        "humidity": state["humidity"] if state["humidity"] is not None else 50.0,
    }


def forecast_boost() -> float:
    """Extra risk points (0..10) from incoming-rain forecast. 0 when disabled/stale."""
    return state["forecast_boost"] if (state["ok"] and _config.get("enabled")) else 0.0


def poll_seconds() -> int:
    return int(_config.get("poll_sec", 600))
