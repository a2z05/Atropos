#!/usr/bin/env python3
"""Atropos fleet — multi-box health grid, stdlib only.

Tracks sibling Atropos boxes in detect.atropos_home()/fleet.json and pings
each one's ``/api/status`` endpoint over HTTP with the shared token.

Storage shape::

    {"boxes": [
        {"id": "f3a2...", "name": "office", "url": "http://host:8787",
         "token": "…", "last_seen": 0.0, "last_status": "ok"},
    ]}

``ping`` never raises: any network error is folded into the result dict.
"""
import json
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import detect

DEFAULT_TIMEOUT = 5.0


def fleet_path() -> Path:
    """Location of the fleet registry file (~/.atropos/fleet.json)."""
    return detect.atropos_home() / "fleet.json"


def _load() -> dict:
    try:
        return json.loads(fleet_path().read_text(encoding="utf-8"))
    except Exception:
        return {"boxes": []}


def _save(data: dict):
    fleet_path().parent.mkdir(parents=True, exist_ok=True)
    fleet_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _now() -> float:
    return time.time()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find(box_id: str):
    for box in _load()["boxes"]:
        if box["id"] == box_id:
            return box
    return None


def add(name: str, url: str, token: str = "") -> dict:
    """Register a fleet box. Validates that url is a http(s) URL.

    Returns the new box dict. Raises ValueError on bad input.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("box name is required")
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError(f"url must be http(s), got: {url!r}")

    data = _load()
    for box in data["boxes"]:
        if box["url"] == url.rstrip("/"):
            raise ValueError(f"a box with url {url!r} is already registered")
    box = {
        "id": secrets.token_hex(6),
        "name": name,
        "url": url.rstrip("/"),
        "token": token,
        "last_seen": None,
        "last_status": "unknown",
    }
    data["boxes"].append(box)
    _save(data)
    return box


def remove(box_id: str) -> dict:
    """Remove a box by id. Returns {ok, removed} or {ok: False, error}."""
    data = _load()
    for i, box in enumerate(data["boxes"]):
        if box["id"] == box_id:
            removed = data["boxes"].pop(i)
            _save(data)
            return {"ok": True, "removed": removed["name"]}
    return {"ok": False, "error": f"box not found: {box_id}"}


def _ping_box(box: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Ping one box's /api/status. Never raises; returns a result dict.

    On success: {ok: True, latency_ms, version, router, error: None}.
    On failure: {ok: False, latency_ms: None, error: "…"}.
    """
    url = box["url"].rstrip("/") + "/api/status"
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        if box.get("token"):
            # assigned directly: Request(headers=...) would lowercase the
            # header name via add_header's capitalize()
            req.headers["X-Atropos-Token"] = box["token"]
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = round((time.monotonic() - t0) * 1000)
            try:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            return {
                "ok": True,
                "latency_ms": latency,
                "version": data.get("version", ""),
                "router": data.get("router", ""),
                "error": None,
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "latency_ms": round((time.monotonic() - t0) * 1000),
                "version": "", "router": "", "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "version": "", "router": "",
                "error": str(e) or type(e).__name__}


def _update_box(box: dict, result: dict):
    """Write a ping result back into the registry (last_seen / last_status)."""
    data = _load()
    for entry in data["boxes"]:
        if entry["id"] == box["id"]:
            entry["last_seen"] = _now() if result.get("ok") else entry.get("last_seen")
            entry["last_status"] = "ok" if result.get("ok") else "down"
            _save(data)
            return


def ping(box_id=None, timeout: float = DEFAULT_TIMEOUT) -> list:
    """Ping one box (by id) or every box when box_id is None.

    Returns a list of {id, name, url, ok, latency_ms, version, router,
    error, status, ts}. Never raises, even when the network is down.
    """
    boxes = _load()["boxes"]
    if box_id is not None:
        boxes = [b for b in boxes if b["id"] == box_id]
    if not boxes:
        return [{"ok": False, "error": "box not found" if box_id else "no boxes registered"}]

    results = []
    for box in boxes:
        result = _ping_box(box, timeout=timeout)
        _update_box(box, result)
        results.append({
            "id": box["id"],
            "name": box["name"],
            "url": box["url"],
            "ok": result.get("ok"),
            "latency_ms": result.get("latency_ms"),
            "version": result.get("version", ""),
            "router": result.get("router", ""),
            "error": result.get("error"),
            "status": "ok" if result.get("ok") else "down",
            "ts": _ts(),
        })
    return results


def status_all() -> list:
    """Return fleet rows without hitting the network.

    Each row: {id, name, url, last_seen, last_status} plus a live ``status``
    derived from last_status (falls back to "unknown").
    """
    rows = []
    for box in _load()["boxes"]:
        rows.append({
            "id": box["id"],
            "name": box["name"],
            "url": box["url"],
            "last_seen": box.get("last_seen"),
            "last_status": box.get("last_status", "unknown"),
            "status": box.get("last_status", "unknown") if box.get("last_seen") else "unknown",
        })
    return rows


def list_boxes() -> list:
    """Return the raw registry entries (no pings)."""
    return list(_load()["boxes"])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "add":
        add(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
    elif len(sys.argv) > 1 and sys.argv[1] == "ping":
        for r in ping(sys.argv[2] if len(sys.argv) > 2 else None):
            print(r)
    else:
        for row in status_all():
            print(row)
