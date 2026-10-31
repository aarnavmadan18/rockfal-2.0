# INNOVATRIX — AI-Based Rockfall Prediction & Alert System (v5.0)

Real, working system: ESP32 sensor nodes → FastAPI backend (risk engine + WebSocket)
→ live 3D dashboard (Three.js). No mock data — everything updates from what your
ESP32 actually sends.

## UI v2 — what's new

- **Control-room theme** — dark industrial look, KPI strip in the header
  (highest risk, mine avg, nodes/cameras online, drone surveys, alerts).
- **Upgraded 3D pit** — 4 terraced benches, shadows + fog, sensor masts that
  glow green when that zone's node is live, zone labels that show the live
  risk % right on the map, **click any zone on the 3D map to focus it**,
  auto-rotate (toggle bottom-right), reset view.
- **Zone Analytics panel** — fused-risk gauge + 4 live trend charts (risk,
  vibration RMS, tilt, rainfall) for the focused zone. Hover any chart for
  exact values with timestamps. History builds up as data streams in.
- **Optional siren** — toggle in the Alert Log header; beeps on High/Critical.
- **Responsive** — works on a phone/tablet on the same WiFi for the demo.
- **Offline-proof** — `three.min.js` is bundled in `frontend/`, so the 3D map
  works even if venue WiFi has no internet (CDN is only a fallback).

## UI v3 — interactive layer

- **▶ Demo scenario button** (bottom-right of the map, or press `D`) — feeds a
  scripted rockfall buildup into the *focused* zone through the **real**
  `/api/sensor-data` pipeline (same endpoint the ESP32 uses), so the risk
  engine, alerts and drone logic all run genuinely. A red **SIMULATION**
  banner stays on screen the whole time — nothing is silently faked. Click
  again to stop; it cools the zone back down on its own.
- **3D hover cards** — hover any zone on the pit for a live status card
  (risk, node/cam status, drone state). Click to focus — the camera smoothly
  flies around to that zone.
- **Survey drone animation** — when a zone crosses risk ≥70 and
  `drone_requested` fires, a little drone lifts out of the pit and hovers
  over that zone until the survey flag clears.
- **Keyboard shortcuts** — `1–6` focus zones, `D` demo, `Space` auto-rotate,
  `R` reset view, `S` siren, `?` quick guide.
- **Quick Guide modal** (`?` button) — includes a scripted "60-second judge
  demo" flow you can literally follow on stage.

## UI v4 — Drone Survey → real 3D reconstruction

New **Drone Survey — 3D Reconstruction** panel (right column). Per zone:

1. **manage → Upload drone images** — the slope photos your drone captured
   (fly with 70–80% overlap between shots for photogrammetry to work).
2. **Reconstruct** — two honest paths:
   - **One-click (NodeODM)**: run the free OpenDroneMap engine once:
     `docker run -ti -p 3001:3000 opendronemap/nodeodm`
     then hit **⚙ Reconstruct 3D model**. The backend submits the images,
     shows live progress in the panel, and auto-downloads the textured
     model when done. (Processing takes ~10–30 min depending on image
     count and PC — this is genuine photogrammetry, not instant.)
   - **Manual (WebODM)**: process in WebODM's UI yourself, then
     **Upload .ply / .glb / .obj(+.mtl+textures)** directly.
3. **◈ view 3D** — fullscreen viewer (orbit/pan/zoom, wireframe toggle)
   renders the zone's actual reconstructed slope.

A synthetic **sample reconstruction** ships in `backend/surveys/sample/`
(benched slope with a visible tension crack) — the **View sample
reconstruction** button demos the whole viewer before any real drone
flight. It's clearly labelled SAMPLE.

New API: `GET /api/surveys`, `POST /api/survey/{zone}/images`,
`POST /api/survey/{zone}/process`, `POST /api/survey/{zone}/model`,
`DELETE /api/survey/{zone}`. Files live under `backend/surveys/<zone>/`.

> Note: real-time 3D from a live drone feed isn't how photogrammetry
> works — reconstruction is per-survey (minutes, not milliseconds). The
> honest pitch: "continuous sensors watch 24×7; when risk spikes, a drone
> survey produces an updated 3D model of that zone for visual confirmation."

## v4.4 — Real-time message alerts (Telegram / Email / SMS / Webhook + device push)

New **Alert Channels** panel (right column, under the Alert Log). When any
zone crosses **High (≥70%)** or **Critical (≥85%)**, INNOVATRIX now messages
real people automatically — not just the screen:

- **Telegram** (free, 2-min setup): message **@BotFather** → `/newbot` →
  paste the bot token in *⚙ Manage channels*. Each recipient sends your bot
  one message, grabs their chat ID from **@userinfobot**, and you add that
  ID as a recipient. Done — they get instant alerts on their phone.
- **Email**: any SMTP account. For Gmail, enable 2-step verification and
  create an **App Password** (myaccount.google.com/apppasswords).
- **SMS**: plug in Twilio credentials (SID / auth token / from-number).
  Optional — skip it if you don't have an account.
- **Webhook**: POST the full alert JSON to any URL — Slack/Discord bots,
  IFTTT, a mine's existing control system.
- **🔔 push** (button in the panel header): native browser notifications on
  the device viewing the dashboard — works on phones on the demo WiFi with
  zero configuration. Clicking the notification flies the 3D camera to the
  affected zone.

Anti-spam logic built in: one message per zone+level per channel every
`cooldown_sec` (default 300 s) — but a level **upgrade** (High → Critical)
always goes out immediately. A **Send test alert** button fires a clearly
labelled drill to every recipient so you can prove the pipeline live on
stage without shaking anything.

Recipients can be filtered per severity (e.g. supervisor gets High+,
mine manager only Critical) — set it when adding them.

Config persists in `backend/notify_config.json` (auto-created; secrets stay
on your machine, never sent to the dashboard). New API:
`GET/POST /api/notify/config`, `POST /api/notify/subscribers`,
`DELETE /api/notify/subscribers/{id}`, `POST /api/notify/test`,
`GET /api/notify/log`. Message sends run on worker threads — sensor
ingestion never blocks on a slow SMTP server.

## v5.0 — Weather intelligence + phone-camera capture

**☁ Live weather + forecast-aware risk (Open-Meteo — free, no API key, no signup):**

- Click the **Weather** KPI chip in the header → set your mine's lat/lon
  (or hit **📍 Use this device's location**) → Save. Done.
- Zones **without** a physical sensor get live rainfall / temperature /
  humidity from the API — marked **☁ API** in the sensor grid, never
  silently faked. The moment a real ESP32 posts data, it overrides the API
  for that zone (**📡 sensor**).
- **Forecast-aware risk:** ≥10 mm of rain expected in the next 6 h shows a
  **🌧 RAIN INCOMING** badge and adds a pre-emptive boost (up to **+10**,
  scaling to 30 mm/6 h) to every zone's risk score. Rockfalls follow rain —
  predicting it beats measuring it. Weather-driven threshold crossings fire
  the full alert pipeline (log, siren, Telegram/SMS/email) like sensor data.
- Refreshes every 10 min (`poll_sec` in `backend/weather_config.json`).
  API: `GET /api/weather`, `POST /api/weather/config`.

**📷 Phone-camera surveys (no drone needed):**

- Survey modal now has **Capture with camera** next to gallery upload. On a
  phone it opens the camera directly; every shot uploads instantly with a
  live counter coaching you to 30+ overlapping photos.
- Then reconstruct as usual (NodeODM one-click, or process in
  KIRI Engine / WebODM and upload the .ply/.glb/.obj).
- Duplicate filenames from phone cameras are auto-uniquified server-side —
  captures never overwrite each other.

## 1. Run the backend

```bash
cd backend
pip install -r requirements.txt
python main.py
```

Server starts at `http://0.0.0.0:8000`. Find your machine's LAN IP:
- Windows: `ipconfig` (look for IPv4 Address)
- Mac/Linux: `ifconfig` or `ip addr`

Example: `192.168.1.50`

## 2. Open the dashboard

On any device on the same WiFi: `http://192.168.1.50:8000`

You'll see the 3D pit map (drag to rotate, scroll to zoom), 6 zones (A–F),
live sensor panel, and alerts feed. It's blank/grey until a zone sends its
first reading.

## 3. "Connect Device" panel (for demo, before hardware is wired up)

The dashboard has a **Device Connections** panel showing all 6 zones as
**Not Connected** until real hardware talks to the backend — nothing is
faked. Two buttons:

- **+ Connect Sensor Node** — pick a zone, it shows you the exact
  `SERVER_URL` / `ZONE_ID` to paste into that ESP32's code. The card
  flips to "Connected" automatically the instant that board's first
  reading arrives — no manual toggle, no restart.
- **+ Connect Camera** — pick a zone, paste a stream URL (e.g. an
  ESP32-CAM's `http://<ip>/stream` MJPEG endpoint) and hit Connect.
  The zone's live feed panel then shows that stream. Leave it for
  later — it's harmless to open the panel and just show judges the
  flow without submitting a URL.

A device auto-marks itself disconnected if it goes quiet for 15
seconds (`DEVICE_TIMEOUT_SEC` in `main.py`), so the panel always
reflects real, current hardware state.

## 4. Flash the ESP32

Open `esp32/sensor_node.ino` in Arduino IDE.

Install libraries (Library Manager): `Adafruit MPU6050`, `Adafruit Unified Sensor`,
`DHT sensor library`, `ArduinoJson`.

Edit these lines at the top:
```cpp
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL    = "http://192.168.1.50:8000/api/sensor-data"; // your backend IP
const char* ZONE_ID       = "A"; // A different letter for each physical node
```

Wiring:
- MPU6050 → ESP32: VCC→3V3, GND→GND, SDA→GPIO21, SCL→GPIO22
- Rain sensor analog out → GPIO34
- DHT22 data → GPIO27 (10k pull-up to 3V3)

Flash it, open Serial Monitor (115200 baud) — you'll see `POST -> 200` once
it's talking to the backend, and the matching zone tile on the dashboard
will update live.

## 5. Multiple zones

Each physical ESP32 node = one zone. Flash 2–3 boards with `ZONE_ID` set to
different letters (A, B, C…) and place them at different points on your
physical slope model — the dashboard tracks each independently.

## How the risk score works

`risk = vibration_RMS×40% + tilt×25% + rainfall×20% + base_geological_risk×15%`

- **Vibration RMS**: rolling root-mean-square of the last 10 accelerometer
  readings (gravity removed) — smooths out single spikes, catches sustained
  shaking.
- **Tilt**: angle of the node relative to vertical, from the gravity vector.
- **Rainfall**: from the analog rain sensor (swap for a real tipping-bucket
  gauge or a weather API in production).
- **Base risk**: static per-zone value in `backend/main.py` (`ZONES` dict) —
  stand-in for a one-time DEM/slope-stability analysis. Replace with real
  values once you run that analysis on your site's DEM.

Score ≥70 → zone auto-flags `drone_requested = true` and fires an alert.
Call `POST /api/drone/{zone_id}/complete?crack_detected=true` once a drone
survey confirms/denies visual instability — this is the hook point for
plugging in a real crack-detection CV model later.

## Swapping in a trained ML model

`compute_risk()` in `backend/main.py` is the only function that needs to
change — replace the weighted formula with `model.predict(features)` from
a trained XGBoost/CNN-LSTM model. Everything else (ingestion, WebSocket,
dashboard, alerts) stays the same.

## What's simulated vs real here

- **Real**: vibration, tilt, rainfall, temp, humidity — all live from your
  ESP32 hardware. Risk scoring, alerting, and the dashboard are fully
  functional, not mocked.
- **Stand-in for now**: drone imagery/crack-detection CV, and per-zone base
  geological risk (hand-set instead of computed from a real DEM). Both have
  a clear integration point (`/api/drone/.../complete`, `ZONES` dict) so you
  can plug in real pipelines without touching the rest of the system.

## Phone-sensor demo (after cloning)

The repository does not include the HTTPS certificate, your alert recipients or survey photos (see `.gitignore`). After cloning:

1. `cd backend` and `py -m pip install -r requirements.txt cryptography`
2. `py make_cert.py` — creates `certs/cert.pem` and `certs/key.pem` for this laptop's IPs and the hotspot address 192.168.137.1
3. Double-click `START_INNOVATRIX.bat` (server on :8000, HTTPS bridge on :8443)
4. Turn on the laptop's Mobile hotspot, connect the phone to it, then open `https://192.168.137.1:8443/sensor.html?zone=F` on the phone (accept the certificate warning, tap ARM)
5. If the phone can't connect, allow the ports once in an administrator PowerShell:
   `New-NetFirewallRule -DisplayName "BhuRakshak demo" -Direction Inbound -Protocol TCP -LocalPort 8000,8443 -Action Allow -Profile Any`
