#!/usr/bin/env python3
"""Atropos watch — self-healing watchdog daemon.

Runs as a long-lived process (or cron), periodically checks:
- Disk usage (alert > 80%, auto-clean temp files)
- Patches (detect drift, re-apply if needed)
- Health (run doctor checks)
- Log rotation (trim old logs)
- State DB (alert if growing too fast)

Notifications: writes to ~/.atropos/watch.log + optional Telegram webhook.

Usage:
  atropos watch              # run once (good for cron)
  atropos watch --daemon     # loop forever with interval
  atropos watch --interval 1800   # custom interval (seconds)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, doctor, patches


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _log(msg, log_path=None):
    line = f"[{_ts()}] {msg}"
    print(line)
    log_path = log_path or (detect.atropos_home() / "watch.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _telegram_notify(text):
    """Send alert to Telegram if configured."""
    try:
        env_path = detect.hermes_home() / ".env"
        if not env_path.exists():
            return
        token = ""
        chat_id = ""
        for line in env_path.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("ATRA_LOG_CHANNEL=") or line.startswith("TELEGRAM_LOG_CHANNEL="):
                chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not token or not chat_id:
            return
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def check_disk():
    """Check disk usage, auto-clean temp files if high."""
    usage = shutil.disk_usage(str(detect.hermes_home()))
    pct = (usage.used / usage.total) * 100
    free_mb = usage.free / (1024 * 1024)
    alert = pct > 80
    cleaned = 0

    if pct > 80:
        # auto-clean: __pycache__, *.pyc, tmp files
        for d in [detect.hermes_home() / "__pycache__", detect.atropos_home() / "__pycache__"]:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                cleaned += 1
        # trim old logs > 10MB
        logs_dir = detect.hermes_home() / "logs"
        if logs_dir.exists():
            for f in logs_dir.glob("*.log"):
                if f.stat().st_size > 10 * 1024 * 1024:
                    # truncate to last 1MB
                    try:
                        content = f.read_bytes()
                        f.write_bytes(content[-1024 * 1024:])
                        cleaned += 1
                    except Exception:
                        pass
        # check again
        usage2 = shutil.disk_usage(str(detect.hermes_home()))
        pct = (usage2.used / usage2.total) * 100
        free_mb = usage2.free / (1024 * 1024)

    return {
        "ok": not alert,
        "pct": round(pct, 1),
        "free_mb": round(free_mb, 1),
        "cleaned": cleaned,
    }


def check_patches():
    """Verify patches haven't drifted from expected state."""
    results = patches.verify()
    failed = [r for r in results if not r.get("applied", False)]
    return {
        "ok": len(failed) == 0,
        "total": len(results),
        "applied": len(results) - len(failed),
        "drifted": [r["id"] for r in failed],
    }


def check_health():
    """Run doctor checks (no fix)."""
    results = doctor.doctor(fix=False)
    failed = [r for r in results if not r["ok"]]
    return {
        "ok": len(failed) == 0,
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": [{"name": r["name"], "msg": r["msg"]} for r in failed],
    }


def check_state_db():
    """Monitor state.db size growth."""
    db = detect.hermes_home() / "state.db"
    if not db.exists():
        return {"ok": True, "size_mb": 0, "msg": "not found"}
    size_mb = db.stat().st_size / (1024 * 1024)
    # count messages
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        msg_count = con.execute("SELECT count(*) FROM messages").fetchone()[0]
        session_count = con.execute("SELECT count(*) FROM sessions").fetchone()[0]
        con.close()
    except Exception:
        msg_count = session_count = 0
    alert = size_mb > 100  # 100MB threshold
    return {
        "ok": not alert,
        "size_mb": round(size_mb, 1),
        "messages": msg_count,
        "sessions": session_count,
    }


def check_log_rotation():
    """Trim excessively large log files."""
    cleaned = []
    logs_dir = detect.hermes_home() / "logs"
    if not logs_dir.exists():
        return {"ok": True, "cleaned": []}
    for f in logs_dir.glob("*.log"):
        size = f.stat().st_size
        if size > 50 * 1024 * 1024:  # 50MB
            try:
                content = f.read_bytes()
                f.write_bytes(content[-2 * 1024 * 1024:])  # keep last 2MB
                cleaned.append(f.name)
            except Exception:
                pass
    return {"ok": len(cleaned) == 0, "cleaned": cleaned}


def run_watch():
    """Run all checks once. Returns summary dict."""
    results = {}
    alerts = []

    # disk
    disk = check_disk()
    results["disk"] = disk
    if not disk["ok"]:
        alerts.append(f"🔴 Disk: {disk['pct']}% used ({disk['free_mb']}MB free), cleaned {disk['cleaned']} files")

    # patches
    patches_result = check_patches()
    results["patches"] = patches_result
    if not patches_result["ok"]:
        alerts.append(f"🔴 Patches drifted: {patches_result['drifted']}")

    # health
    health = check_health()
    results["health"] = health
    if not health["ok"]:
        names = [f["name"] for f in health["failed"]]
        alerts.append(f"🔴 Health: {names} failed")

    # state db
    state = check_state_db()
    results["state_db"] = state
    if not state["ok"]:
        alerts.append(f"🟡 State DB: {state['size_mb']}MB ({state['messages']} msgs)")

    # log rotation
    logs = check_log_rotation()
    results["logs"] = logs
    if not logs["ok"]:
        alerts.append(f"🟡 Logs trimmed: {logs['cleaned']}")

    results["alerts"] = alerts
    results["ok"] = len(alerts) == 0
    results["ts"] = _ts()

    # log result
    if alerts:
        for a in alerts:
            _log(a)
        # notify telegram
        _telegram_notify("⚠️ <b>Atropos Watch</b>\n" + "\n".join(alerts))
    else:
        _log(f"✅ All checks passed (disk={disk['pct']}%, patches={patches_result['applied']}/{patches_result['total']}, health={health['passed']}/{health['total']})")

    return results


def daemon_loop(interval=1800):
    """Run checks in a loop."""
    _log(f"🔴 Atropos Watch daemon started (interval={interval}s)")
    while True:
        try:
            run_watch()
        except Exception as e:
            _log(f"🔴 Watch error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=1800)
    args = ap.parse_args()
    if args.daemon:
        daemon_loop(args.interval)
    else:
        results = run_watch()
        print(json.dumps(results, indent=2, ensure_ascii=False))
