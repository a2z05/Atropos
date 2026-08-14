#!/usr/bin/env python3
"""Atropos logs — gateway log tailing, stdlib only."""
from pathlib import Path

from . import detect


def latest_log_file() -> Path:
    """Return the most recently modified .log file under hermes_home()/logs."""
    logs_dir = detect.hermes_home() / "logs"
    if not logs_dir.exists():
        return None
    log_files = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    return log_files[-1] if log_files else None


def tail(n=80):
    """Tail N lines of the latest gateway log.

    Returns (file_name, lines) or (None, []) when no log exists.
    """
    target = latest_log_file()
    if not target:
        return None, []
    try:
        with open(target, "r", errors="replace") as f:
            all_lines = f.readlines()
        return target.name, all_lines[-n:]
    except OSError:
        return None, []
