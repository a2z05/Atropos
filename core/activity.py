#!/usr/bin/env python3
"""Atropos activity — append-only timeline, stdlib only.

Every event is one JSON line appended to detect.atropos_home()/activity.jsonl::

    {"ts": "2026-08-15T12:00:00Z", "event": "update", "detail": "…"}

The file auto-rotates at settings ``activity.max_mb`` (default 5 MB):
when a write would exceed the cap, the file is truncated to the last
1 MB (whole lines only). ``feed()`` groups events into per-category
counts for the dashboard activity panel.
"""
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import detect, settings

ACTIVITY_FILE = "activity.jsonl"
DEFAULT_MAX_MB = 5
ROTATION_KEEP_BYTES = 1024 * 1024  # 1 MB kept after rotation
MAX_TODAY = 500
CATEGORY_EVENTS = {
    "updates": ("update", "apply"),
    "alerts": ("alert",),
    "backups": ("backup",),
    "jailbreaks": ("jailbreak",),
    "sessions": ("session",),
    "routers": ("router", "fleet"),
}


def activity_path() -> Path:
    """Location of the activity timeline file."""
    return detect.atropos_home() / ACTIVITY_FILE


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_mb() -> int:
    try:
        return max(1, int(settings.get("activity.max_mb", DEFAULT_MAX_MB) or DEFAULT_MAX_MB))
    except Exception:
        return DEFAULT_MAX_MB


def _category(event: str) -> str:
    ev = (event or "").lower()
    for cat, keys in CATEGORY_EVENTS.items():
        if any(k in ev for k in keys):
            return cat
    return "events"


def _rotate_if_needed(path: Path, max_mb: int):
    """Truncate the timeline to the last 1 MB when it exceeds max_mb."""
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < max_mb * 1024 * 1024:
        return
    try:
        keep = ROTATION_KEEP_BYTES
        with open(path, "rb") as f:
            f.seek(max(0, size - keep))
            tail = f.read()
        first_nl = tail.find(b"\n")
        if first_nl != -1:
            tail = tail[first_nl + 1:]
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(tail)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def log(event: str, detail: str = "") -> dict:
    """Append one activity entry. Returns the stored line dict."""
    entry = {"ts": _ts(), "event": str(event or "unknown"), "detail": str(detail or "")}
    path = activity_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path, _max_mb())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _read_entries() -> list:
    path = activity_path()
    if not path.exists():
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if isinstance(data, dict) and "ts" in data and "event" in data:
                    entries.append(data)
    except OSError:
        return []
    return entries


def _parse_ts(value) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def today() -> list:
    """Entries from the last 24 hours, newest first, bounded to 500."""
    cutoff = time.time() - 24 * 3600
    entries = [e for e in _read_entries() if _parse_ts(e.get("ts", "")) >= cutoff]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:MAX_TODAY]


def feed() -> dict:
    """Grouped activity summary: per-category counts + recent events.

    Returns {updates, alerts, backups, jailbreaks, sessions, routers,
    events: [most recent 20]} where ``events`` lists the raw entries that
    didn't match a category.
    """
    entries = today()
    counts = {cat: 0 for cat in CATEGORY_EVENTS}
    events = []
    for e in entries:
        cat = _category(e.get("event", ""))
        if cat == "events":
            events.append(e)
        else:
            counts[cat] += 1
    counts["events"] = events[:20]
    return counts


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "log":
        print(json.dumps(log(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""),
                         indent=2, ensure_ascii=False))
    else:
        print(json.dumps(feed(), indent=2, ensure_ascii=False))
