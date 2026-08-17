#!/usr/bin/env python3
"""Railway cloud integration — status, health, deploy lifecycle.

v18 B: cloud-native features for Railway deploys. Everything here is
gated on ``detect.detect_cloud() == "railway"`` and reads Railway's
environment contract (RAILWAY_* vars). Network posture is outbound-only:
Railway blocks inbound SSH and most inbound traffic (v18 H2), so this
module never listens for inbound connections — deploy tracking is
poll-based via ``RAILWAY_GIT_COMMIT_SHA`` at startup, and the deploy
webhook is simulated by the same poll when the commit changes.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import detect, snapshots


def _env(name: str, default: str = "") -> str:
    import os
    return os.environ.get(name, default)


def status() -> dict:
    """Project/service/env/domain/volume/replica/git facts from env."""
    return {
        "ok": True,
        "cloud": detect.detect_cloud(),
        "project": _env("RAILWAY_PROJECT_ID"),
        "service": _env("RAILWAY_SERVICE_ID"),
        "environment": _env("RAILWAY_ENVIRONMENT_ID"),
        "domain": _env("RAILWAY_PUBLIC_DOMAIN"),
        "volume": _env("RAILWAY_VOLUME_MOUNT_PATH"),
        "replica": _env("RAILWAY_REPLICA_ID"),
        "git_commit": _env("RAILWAY_GIT_COMMIT_SHA"),
        "git_branch": _env("RAILWAY_GIT_BRANCH"),
    }


def is_railway() -> bool:
    return detect.detect_cloud() == "railway"


def volume_usage() -> dict:
    """Used % of the /data (volume) mount, warned >80%."""
    import shutil
    mount = _env("RAILWAY_VOLUME_MOUNT_PATH") or "/data"
    try:
        du = shutil.disk_usage(mount)
        pct = round(du.used / du.total * 100, 1)
        return {"ok": True, "mount": mount, "used_pct": pct,
                "used": du.used, "total": du.total, "warn": pct > 80}
    except (OSError, ValueError):
        return {"ok": False, "mount": mount, "error": "volume not mounted"}


# ── deploy tracking ────────────────────────────────────────────────────────
def _state_path() -> Path:
    return detect.atropos_home() / "railway_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(data: dict):
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def deploy_commit() -> str:
    """Current deploy's git sha (empty off-Railway)."""
    return _env("RAILWAY_GIT_COMMIT_SHA")


def check_deploy() -> dict:
    """Once per process start: if the commit changed, snapshot + backup.

    Returns {ok, changed, sha, snapshot_id, backup_id, skipped} — skipped
    with reason when not on Railway or the commit is unchanged.
    """
    sha = deploy_commit()
    if not is_railway() or not sha:
        return {"ok": True, "changed": False, "skipped": "not on railway"}
    state = _load_state()
    last = state.get("last_deploy_sha")
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = {"ok": True, "changed": last != sha, "sha": sha}
    if last == sha:
        out["skipped"] = "no commit change"
        return out
    # ── deploy lifecycle: named snapshot + auto-backup before new code ──
    try:
        snap = snapshots.create(f"deploy-{sha[:8]}-{now}")
        out["snapshot_id"] = snap.get("id") or snap.get("name")
    except Exception as e:
        out["snapshot_error"] = str(e)
    try:
        from . import backup
        res = backup.create()
        out["backup_id"] = res.get("id") or res.get("path")
        out["backup_ok"] = bool(res.get("ok"))
    except Exception as e:
        out["backup_error"] = str(e)
    state["last_deploy_sha"] = sha
    state["last_deploy_time"] = _now()
    _save_state(state)
    return out


def last_deploy() -> dict:
    """Last deploy backup info for the dashboard banner strip."""
    state = _load_state()
    if not state.get("last_deploy_time"):
        return {"ok": True, "present": False}
    return {"ok": True, "present": True, "sha": state.get("last_deploy_sha"),
            "at": state.get("last_deploy_time")}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def doctor_extra() -> list:
    """Extra doctor checks when on Railway: volume mount + stale PIDs."""
    checks = []
    vu = volume_usage()
    checks.append({
        "name": "railway volume",
        "ok": vu.get("ok") and not vu.get("warn"),
        "detail": (f"{vu.get('used_pct')}% used" if vu.get("ok")
                   else vu.get("error", "volume unavailable")),
    })
    stale = _stale_pid_count()
    checks.append({
        "name": "stale pids",
        "ok": stale == 0,
        "detail": f"{stale} stale" if stale else "clean",
    })
    return checks


def _stale_pid_count() -> int:
    """Count leftover .pid files in ~/.atropos that point at dead processes."""
    import os
    home = detect.atropos_home()
    count = 0
    try:
        for p in home.glob("*.pid"):
            try:
                pid = int(p.read_text().strip())
            except (ValueError, OSError):
                count += 1
                continue
            try:
                os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                count += 1
    except OSError:
        pass
    return count


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "volume":
        print(json.dumps(volume_usage(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "deploy":
        print(json.dumps(check_deploy(), indent=2))
    else:
        print(json.dumps(status(), indent=2))