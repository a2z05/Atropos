#!/usr/bin/env python3
"""Atropos update — atomic updater: fetch upstream → diff → backup → reset →
apply hacks → doctor verify → rollback on failure."""
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import detect, doctor, patches


def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def fetch_upstream(repo: str, timeout=120):
    """git fetch + log. Returns (head, commits_since) or raises."""
    out = subprocess.run(
        ["git", "-C", repo, "fetch", "origin", "main"],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(f"fetch failed: {out.stderr[:300]}")
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--short", "origin/main"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    count = subprocess.run(
        ["git", "-C", repo, "rev-list", "--count", "HEAD..origin/main"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip() or "0"
    return {"head": head, "remote": remote, "behind": count}


def diff_summary(repo: str):
    """What changed between HEAD and origin/main (file list + size)."""
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--stat", "HEAD..origin/main"],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def backup_state(atropos_home: Path) -> Path:
    """Copy config + hacks to a timestamped backup dir. Returns backup path."""
    ts = _ts()
    bk = atropos_home / "backups" / ts
    bk.mkdir(parents=True, exist_ok=True)
    cfg = atropos_home / "config.yaml"
    if cfg.exists():
        shutil.copy2(cfg, bk / "config.yaml")
    hacks = Path(__file__).resolve().parent.parent / "hacks"
    if hacks.exists():
        shutil.copytree(hacks, bk / "hacks", dirs_exist_ok=True)
    return bk


def _git(repo: str, *args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def apply_update(repo: str, dry_run=False):
    """Full update: fetch → backup → reset → re-apply hacks → doctor verify.

    Returns summary dict. Rollback to the pre-update HEAD on failure.
    """
    if dry_run:
        return {"dry_run": True, **fetch_upstream(repo)}

    home = detect.atropos_home()
    # 1. fetch + record the pre-update head for rollback
    up = fetch_upstream(repo)
    prev_head = up["head"]
    if up["head"] == up["remote"]:
        return {**up, "ok": True, "up_to_date": True}

    # 2. backup config + hacks
    bk = backup_state(home)

    # 3. reset to upstream
    out = _git(repo, "reset", "--hard", "origin/main")
    if out.returncode != 0:
        return {"ok": False, "error": f"reset failed: {out.stderr[:300]}", "backup": str(bk)}

    # 4. re-apply hacks
    applied, skipped, errors = patches.apply_hacks()
    ok = not errors

    # 5. doctor verify (only the checks that matter post-patch)
    checks = [c for c in doctor.doctor() if c["name"] in ("patches", "python >= 3.10")]
    failed_checks = [c for c in checks if not c["ok"]]

    if ok and failed_checks:
        ok = False
        errors = errors + [f"doctor: {c['name']}: {c['msg']}" for c in failed_checks]

    result = {
        "ok": ok,
        "head": fetch_upstream(repo)["head"],
        "prev_head": prev_head,
        "behind": up["behind"],
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "doctor": checks,
        "backup": str(bk),
    }
    if not ok:
        # rollback: restore pre-update head, then re-apply hacks again
        _git(repo, "reset", "--hard", prev_head)
        patches.apply_hacks()
        result["rolled_back"] = True
    return result


def update_check(repo: str):
    """Non-destructive check. Returns dict with what would change."""
    try:
        up = fetch_upstream(repo)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if up["head"] == up["remote"]:
        return {"ok": True, "up_to_date": True, **up}
    return {
        "ok": True,
        "up_to_date": False,
        **up,
        "diff": diff_summary(repo),
    }
