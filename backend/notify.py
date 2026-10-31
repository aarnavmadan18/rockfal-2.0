"""
INNOVATRIX — real-time alert dispatch engine.

Sends High/Critical rockfall alerts to real people over:
  - Telegram  (free — create a bot with @BotFather, paste the token)
  - Email     (any SMTP account, e.g. Gmail app password)
  - SMS       (Twilio — optional, needs account SID/token)
  - Webhook   (generic JSON POST — Slack/Discord/IFTTT/your own server)

Design:
  - Config + subscribers persist in backend/notify_config.json
  - Per-zone/per-level cooldown so a shaking sensor doesn't spam 100 SMSes
    (a level UPGRADE, e.g. High -> Critical, always bypasses cooldown)
  - All network sends run on a worker thread — never blocks sensor ingestion
  - Every attempt is recorded in a dispatch log the dashboard can show
"""

import json
import os
import smtplib
import ssl
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from email.mime.text import MIMEText

try:
    import requests
except ImportError:
    requests = None

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify_config.json")

LEVEL_RANK = {"Low": 0, "Moderate": 1, "High": 2, "Critical": 3}

DEFAULT_CONFIG = {
    "enabled": True,
    "min_level": "High",          # global floor: never message below this
    "cooldown_sec": 300,           # per zone+level, per channel type
    "channels": {
        "telegram": {"bot_token": ""},
        "email": {
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
            "username": "",
            "password": "",        # Gmail: use an App Password, not your real one
            "from_name": "INNOVATRIX Alerts",
        },
        "sms": {
            "twilio_sid": "",
            "twilio_token": "",
            "from_number": "",     # your Twilio number, e.g. +1415xxxxxxx
        },
    },
    # each: {id, type: telegram|email|sms|webhook, target, name, min_level, zones: ["*"] or ["A","B"]}
    "subscribers": [],
}

_lock = threading.Lock()
_config: dict = {}
_last_sent: dict = {}                 # (zone_id, level, channel_type) -> monotonic ts
dispatch_log: deque = deque(maxlen=100)
_broadcast_cb = None                  # set by init(); called with dict from worker threads


# --------------------------------------------------------------------------
# config persistence
# --------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    global _config
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:
            print(f"[notify] bad notify_config.json ({e}) — using defaults")
    _config = cfg
    return _config


def save_config():
    with _lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(_config, f, indent=2)


def get_config_masked() -> dict:
    """Config safe to show in the UI — secrets replaced with '•set•' flags."""
    cfg = json.loads(json.dumps(_config))
    ch = cfg["channels"]
    for key in ("bot_token",):
        ch["telegram"][key + "_set"] = bool(ch["telegram"].get(key))
        ch["telegram"][key] = ""
    for key in ("password",):
        ch["email"][key + "_set"] = bool(ch["email"].get(key))
        ch["email"][key] = ""
    for key in ("twilio_sid", "twilio_token"):
        ch["sms"][key + "_set"] = bool(ch["sms"].get(key))
        ch["sms"][key] = ""
    return cfg


def update_config(patch: dict):
    """Merge a partial config from the UI. Empty-string secrets mean 'keep existing'."""
    global _config
    keep_if_empty = [
        ("channels", "telegram", "bot_token"),
        ("channels", "email", "password"),
        ("channels", "sms", "twilio_sid"),
        ("channels", "sms", "twilio_token"),
    ]
    saved = {}
    for path in keep_if_empty:
        node, cur = patch, _config
        ok = True
        for p in path[:-1]:
            if not isinstance(node, dict) or p not in node:
                ok = False
                break
            node = node[p]
            cur = cur.get(p, {})
        if ok and isinstance(node, dict) and node.get(path[-1], None) == "":
            saved[path] = cur.get(path[-1], "")
    _config = _deep_merge(_config, patch)
    for path, val in saved.items():
        node = _config
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = val
    save_config()
    return get_config_masked()


# --------------------------------------------------------------------------
# subscribers
# --------------------------------------------------------------------------

def add_subscriber(sub: dict) -> dict:
    sub = {
        "id": str(uuid.uuid4())[:8],
        "type": sub.get("type", "telegram"),
        "target": str(sub.get("target", "")).strip(),
        "name": sub.get("name", "").strip() or sub.get("target", ""),
        "min_level": sub.get("min_level", "High"),
        "zones": sub.get("zones", ["*"]),
    }
    if sub["type"] not in ("telegram", "email", "sms", "webhook"):
        raise ValueError(f"unknown subscriber type '{sub['type']}'")
    if not sub["target"]:
        raise ValueError("target is required")
    _config["subscribers"].append(sub)
    save_config()
    return sub


def remove_subscriber(sub_id: str) -> bool:
    before = len(_config["subscribers"])
    _config["subscribers"] = [s for s in _config["subscribers"] if s["id"] != sub_id]
    save_config()
    return len(_config["subscribers"]) < before


# --------------------------------------------------------------------------
# channel senders (each returns (ok: bool, detail: str))
# --------------------------------------------------------------------------

def _send_telegram(target: str, text: str):
    token = _config["channels"]["telegram"].get("bot_token", "")
    if not token:
        return False, "no bot token configured"
    if requests is None:
        return False, "python 'requests' not installed"
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": target, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    if r.ok and r.json().get("ok"):
        return True, "delivered"
    return False, f"telegram API: {r.status_code} {r.text[:120]}"


def _send_email(target: str, text: str, subject: str):
    ch = _config["channels"]["email"]
    if not ch.get("username") or not ch.get("password"):
        return False, "SMTP username/password not configured"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f'{ch.get("from_name", "INNOVATRIX")} <{ch["username"]}>'
    msg["To"] = target
    port = int(ch.get("smtp_port", 465))
    host = ch.get("smtp_host", "smtp.gmail.com")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as s:
                s.login(ch["username"], ch["password"])
                s.sendmail(ch["username"], [target], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(ch["username"], ch["password"])
                s.sendmail(ch["username"], [target], msg.as_string())
        return True, "delivered"
    except Exception as e:
        return False, f"SMTP: {e}"


def _send_sms(target: str, text: str):
    ch = _config["channels"]["sms"]
    sid, token, from_no = ch.get("twilio_sid"), ch.get("twilio_token"), ch.get("from_number")
    if not (sid and token and from_no):
        return False, "Twilio SID/token/from_number not configured"
    if requests is None:
        return False, "python 'requests' not installed"
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={"From": from_no, "To": target, "Body": text[:1500]},
        timeout=15,
    )
    if r.status_code in (200, 201):
        return True, "queued by Twilio"
    return False, f"Twilio: {r.status_code} {r.text[:120]}"


def _send_webhook(target: str, payload: dict):
    if requests is None:
        return False, "python 'requests' not installed"
    try:
        r = requests.post(target, json=payload, timeout=10)
        return (r.status_code < 300), f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"webhook: {e}"


# --------------------------------------------------------------------------
# message formatting
# --------------------------------------------------------------------------

EMOJI = {"Moderate": "⚠️", "High": "\U0001f6a8", "Critical": "\U0001f198"}

def format_alert_text(alert: dict) -> str:
    e = EMOJI.get(alert["level"], "⚠️")
    action = {
        "Critical": "EVACUATE the zone and halt operations immediately.",
        "High": "Clear personnel from the bench; drone survey requested.",
    }.get(alert["level"], "Monitor closely.")
    return (
        f"{e} INNOVATRIX {alert['level'].upper()} ALERT\n"
        f"Zone: {alert['zone_name']}\n"
        f"Risk score: {alert['score']}%\n"
        f"Cause: {alert['reason']}\n"
        f"Time: {alert['date']} {alert['time']}\n"
        f"Action: {action}"
    )


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------

def init(broadcast_cb=None):
    """Call once at startup. broadcast_cb(dict) is invoked (thread-safe by the
    caller's design) after each dispatch batch so the dashboard updates live."""
    global _broadcast_cb
    _broadcast_cb = broadcast_cb
    load_config()
    n = len(_config["subscribers"])
    print(f"[notify] alert dispatch ready — {n} subscriber(s), min level {_config['min_level']}")


def _log(entry: dict):
    entry["ts"] = datetime.now().strftime("%H:%M:%S")
    dispatch_log.appendleft(entry)


def _dispatch_worker(alert: dict, subs: list, is_test: bool):
    text = format_alert_text(alert)
    subject = f"[INNOVATRIX] {alert['level']} alert — {alert['zone_name']} ({alert['score']}%)"
    results = []
    for s in subs:
        t = s["type"]
        if t == "telegram":
            ok, detail = _send_telegram(s["target"], text.replace("INNOVATRIX", "<b>INNOVATRIX</b>", 1))
        elif t == "email":
            ok, detail = _send_email(s["target"], text, subject)
        elif t == "sms":
            ok, detail = _send_sms(s["target"], text)
        elif t == "webhook":
            ok, detail = _send_webhook(s["target"], {"event": "rockfall_alert", **alert, "text": text})
        else:
            ok, detail = False, "unknown type"
        entry = {
            "alert_id": alert.get("id"),
            "zone_id": alert.get("zone_id"),
            "level": alert.get("level"),
            "channel": t,
            "to": s.get("name") or s["target"],
            "ok": ok,
            "detail": detail,
            "test": is_test,
        }
        _log(entry)
        results.append(entry)
        print(f"[notify] {t} -> {entry['to']}: {'OK' if ok else 'FAIL'} ({detail})")
    if _broadcast_cb:
        try:
            _broadcast_cb({"type": "notify_dispatch", "results": results})
        except Exception as e:
            print(f"[notify] broadcast callback failed: {e}")


def _eligible_subscribers(alert: dict) -> list:
    lvl = LEVEL_RANK.get(alert["level"], 0)
    out = []
    for s in _config["subscribers"]:
        if lvl < LEVEL_RANK.get(s.get("min_level", "High"), 2):
            continue
        zones = s.get("zones", ["*"])
        if "*" not in zones and alert["zone_id"] not in zones:
            continue
        out.append(s)
    return out


def dispatch(alert: dict, force: bool = False, prev_level: str = None):
    """Fire-and-forget: decide who gets this alert and send on a worker thread.

    Cooldown: one message per (zone, level, channel-type) per cooldown_sec.
    A level UPGRADE (prev_level rank < new rank) always goes out.
    """
    if not _config.get("enabled", True):
        return {"dispatched": 0, "reason": "notifications disabled"}
    if LEVEL_RANK.get(alert["level"], 0) < LEVEL_RANK.get(_config.get("min_level", "High"), 2):
        return {"dispatched": 0, "reason": f"below min level {_config.get('min_level')}"}

    subs = _eligible_subscribers(alert)
    if not subs:
        return {"dispatched": 0, "reason": "no matching subscribers"}

    upgraded = prev_level is not None and LEVEL_RANK.get(alert["level"], 0) > LEVEL_RANK.get(prev_level, 0)
    now = time.monotonic()
    cooldown = float(_config.get("cooldown_sec", 300))
    to_send = []
    with _lock:
        for s in subs:
            key = (alert["zone_id"], alert["level"], s["type"])
            if force or upgraded or (now - _last_sent.get(key, -1e9)) >= cooldown:
                to_send.append(s)
                _last_sent[key] = now
    if not to_send:
        return {"dispatched": 0, "reason": "cooldown active"}

    threading.Thread(target=_dispatch_worker, args=(alert, to_send, force), daemon=True).start()
    return {"dispatched": len(to_send), "reason": "sending"}


def send_test(level: str = "Critical"):
    """Send a clearly-labelled TEST alert to every subscriber, ignoring cooldowns."""
    alert = {
        "id": "test-" + str(uuid.uuid4())[:6],
        "zone_id": "TEST",
        "zone_name": "TEST — this is a drill",
        "level": level,
        "score": 91.5,
        "reason": "manual test from dashboard (not a real event)",
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
    }
    subs = list(_config["subscribers"])
    if not subs:
        return {"dispatched": 0, "reason": "no subscribers configured"}
    threading.Thread(target=_dispatch_worker, args=(alert, subs, True), daemon=True).start()
    return {"dispatched": len(subs), "reason": "sending test"}
