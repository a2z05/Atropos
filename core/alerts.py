#!/usr/bin/env python3
"""Atropos alerting — Telegram notifications for critical events.

Sends alerts to a Telegram chat (owner or log channel) when:
- Disk usage > threshold (default 80%)
- Doctor check fails
- Router is down / high latency
- Patch verify fails
- Update available or applied

Config (in ~/.atropos/config.yaml):
  alerts:
    enabled: true
    token: <bot token>
    chat_id: <owner or channel id>
    thresholds:
      disk: 80
      latency_ms: 5000
"""
import json
import time
import urllib.request
import urllib.parse

from . import config, detect, settings

ALERT_STATE_FILE = "alert_state.json"


def _bot_config():
    enabled = settings.get("alerts.enabled", True)
    token = settings.get("alerts.token", "") or (
        detect.hermes_home().joinpath("config.yaml").exists() and _hermes_token()
    )
    chat_id = settings.get("alerts.chat_id", "")
    return enabled, token, chat_id


def _hermes_token():
    """Try to read bot token from hermes config."""
    try:
        import re
        text = (detect.hermes_home() / "config.yaml").read_text()
        m = re.search(r"token:\s*['\"]?([0-9]{8,10}:[A-Za-z0-9_-]{30,})", text)
        return m.group(1) if m else None
    except Exception:
        return None


def _state_path():
    return detect.atropos_home() / ALERT_STATE_FILE


def _load_state():
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _save_state(state):
    _state_path().write_text(json.dumps(state, indent=2))


def send_alert(message: str, force: bool = False) -> bool:
    """Send a Telegram alert. Rate-limited to 1 per key per 10 min unless force."""
    enabled, token, chat_id = _bot_config()
    if not enabled:
        return False
    if not token or not chat_id:
        print(f"  [alerts] no token/chat_id configured — cannot send: {message[:80]}")
        return False

    key = message.split(":")[0][:40]
    state = _load_state()
    now = time.time()
    last = state.get(key, 0)
    min_interval = settings.get("alerts.min_interval", 600)
    if not force and now - last < min_interval:
        return False  # rate-limited

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": f"⚠️ ATROPOS: {message}",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, timeout=15)
        with urllib.request.urlopen(req) as resp:
            state[key] = now
            _save_state(state)
            return resp.status == 200
    except Exception as e:
        print(f"  [alerts] send failed: {e}")
        return False


def check_and_alert(doctor_results=None, disk_pct=None, router_status=None, patch_results=None) -> list:
    """Run checks and send alerts for critical issues. Returns alerts sent.

    Each event type is gated by its own settings toggle
    (alerts.events.disk/doctor/router/patches).
    """
    sent = []

    # disk
    if disk_pct is not None and settings.get("alerts.events.disk", True):
        threshold = settings.get("alerts.threshold_disk", 80)
        if disk_pct >= threshold:
            if send_alert(f"⚠️ Disk at {disk_pct:.0f}% (threshold {threshold}%) — clean up!"):
                sent.append(f"disk:{disk_pct:.0f}%")

    # doctor
    if doctor_results and settings.get("alerts.events.doctor", True):
        failed = [r for r in doctor_results if not r["ok"]]
        if failed:
            names = ", ".join(r["name"] for r in failed)
            if send_alert(f"❌ Doctor failed: {names}"):
                sent.append(f"doctor:{names}")

    # router
    if router_status and settings.get("alerts.events.router", True):
        down = [r for r in router_status if not r.get("ok")]
        if down:
            names = ", ".join(r["name"] for r in down)
            if send_alert(f"🔌 Router down: {names}"):
                sent.append(f"router:{names}")

    # patches
    if patch_results is not None and settings.get("alerts.events.patches", True):
        failed = [r for r in patch_results if not r.get("applied")]
        if failed:
            names = ", ".join(r["id"] for r in failed)
            if send_alert(f"🧩 Patches failed: {names}"):
                sent.append(f"patches:{names}")

    return sent


def notify_available() -> None:
    """Notify owner that an update is available."""
    if send_alert("🔄 Update available! Run `atropos update`"):
        print("  [alerts] update notification sent")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send test alert")
    args = ap.parse_args()
    if args.test:
        ok = send_alert("Test alert from Atropos ✅ (if you see this, alerting works)", force=True)
        print(f"  test alert sent: {ok}")
    else:
        print("  use --test to send a test alert")