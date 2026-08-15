#!/usr/bin/env python3
"""Atropos universal webhook registry — one list over every event sink.

Keeps ``~/.atropos/webhooks.json`` — a list of::

    {
      "name": "alerts",
      "url": "https://example.com/hook",
      "events": ["alerts", "backup"],
      "enabled": true,
      "mode": "shared",
      "last_sent": "2026-08-15T12:00:00Z",
      "last_status": 200
    }

``trigger(event, payload)`` POSTs a JSON body to every *enabled* hook that
subscribes to the event. Delivery is per-hook isolated — one failing hook
never raises or blocks the others; failures are collected and returned.
``ping(name)`` sends the conventional ``{"event": "ping"}`` test payload.

Pure stdlib (urllib), never imports core.dashboard (circular).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import detect

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

MODES = ("shared", "per-harness", "atropos-only")
TIMEOUT = 8  # per-hook HTTP timeout in seconds


def valid_name(name: str) -> bool:
    """True when ``name`` is a safe webhook identifier (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name))


def store_path() -> Path:
    """Canonical registry file (~/.atropos/webhooks.json)."""
    return detect.atropos_home() / "webhooks.json"


def _load() -> list:
    """Load hooks; missing/corrupt files yield an empty list."""
    p = store_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [h for h in data if isinstance(h, dict)]


def _save(hooks: list):
    """Write hooks, creating ~/.atropos on demand."""
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hooks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hook(hooks: list, name: str) -> dict | None:
    """Find a hook by name."""
    for h in hooks:
        if h.get("name") == name:
            return h
    return None


def _now_iso() -> str:
    """UTC ISO timestamp for delivery records."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_url(url: str) -> bool:
    """http(s) URL, with a host that is not a filesystem-ish path."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    rest = url.split("://", 1)[1]
    if "/" in rest and not rest.split("/", 1)[0]:
        return False
    if not rest.split("/", 1)[0]:
        return False
    return True


# ── CRUD ──────────────────────────────────────────────────────────────────
def list_webhooks() -> list:
    """All registered webhooks (deep copy)."""
    return json.loads(json.dumps(_load()))


def add(name: str, url: str, events: list | None = None, mode: str = "shared") -> dict:
    """Register a webhook. Raises ValueError on invalid name/url/duplicates."""
    if not valid_name(name):
        raise ValueError(f"invalid webhook name: {name!r}")
    if not _valid_url(url):
        raise ValueError("url must be an http:// or https:// URL")
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    if events is None:
        events = []
    events = [str(e) for e in events if str(e).strip()]
    hooks = _load()
    if _hook(hooks, name) is not None:
        raise ValueError(f"webhook already registered: {name}")
    hook = {
        "name": name,
        "url": url,
        "events": events,
        "enabled": True,
        "mode": mode,
        "last_sent": None,
        "last_status": None,
    }
    hooks.append(hook)
    _save(hooks)
    return dict(hook)


def remove(name: str) -> dict:
    """Remove a webhook permanently from the registry."""
    if not valid_name(name):
        raise ValueError(f"invalid webhook name: {name!r}")
    hooks = _load()
    if _hook(hooks, name) is None:
        raise FileNotFoundError(f"webhook not found: {name}")
    hooks = [h for h in hooks if h.get("name") != name]
    _save(hooks)
    return {"ok": True, "name": name, "removed": True}


def enable(name: str) -> dict:
    """Enable a webhook (it will receive future triggers)."""
    return _set_enabled(name, True)


def disable(name: str) -> dict:
    """Disable a webhook (it stops receiving triggers)."""
    return _set_enabled(name, False)


def _set_enabled(name: str, value: bool) -> dict:
    if not valid_name(name):
        raise ValueError(f"invalid webhook name: {name!r}")
    hooks = _load()
    h = _hook(hooks, name)
    if h is None:
        raise FileNotFoundError(f"webhook not found: {name}")
    h["enabled"] = value
    _save(hooks)
    return {"ok": True, "name": name, "enabled": value}


# ── delivery ──────────────────────────────────────────────────────────────
def _post(url: str, payload: dict) -> tuple:
    """POST JSON to one url. Returns (ok, status, error). Never raises."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return True, resp.status, None
    except urllib.error.HTTPError as err:
        try:
            code = err.code
            reason = err.reason
            err.close()  # release the response body file
        except Exception:
            code, reason = getattr(err, "code", None), getattr(err, "reason", None)
        return code < 500, code, f"HTTP {code}: {reason}"
    except urllib.error.URLError as err:
        return False, None, str(err.reason or err)
    except Exception as exc:
        return False, None, str(exc)


def trigger(event: str, payload: dict | None = None) -> dict:
    """Deliver one event to every enabled hook subscribed to it.

    Returns ``{"event": ..., "delivered": [...], "skipped": [...],
    "failed": [...], "errors": [...]}``. Per-hook try/except isolation —
    a failing hook never crashes the call or blocks the others.
    """
    if payload is None:
        payload = {}
    body = dict(payload)
    body.setdefault("event", event)
    delivered, skipped, failed, errors = [], [], [], []
    hooks = _load()
    changed = False
    for h in hooks:
        if not h.get("enabled"):
            skipped.append(h["name"])
            continue
        if event not in (h.get("events") or []):
            skipped.append(h["name"])
            continue
        ok, status, err = _post(h["url"], body)
        h["last_sent"] = _now_iso()
        h["last_status"] = status
        changed = True
        if ok:
            delivered.append(h["name"])
        else:
            failed.append(h["name"])
            errors.append({"name": h["name"], "error": err})
    if changed:
        _save(hooks)
    return {
        "event": event,
        "delivered": delivered,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }


def ping(name: str) -> dict:
    """Test one webhook with the conventional ``{"event": "ping"}`` body."""
    if not valid_name(name):
        raise ValueError(f"invalid webhook name: {name!r}")
    hooks = _load()
    h = _hook(hooks, name)
    if h is None:
        raise FileNotFoundError(f"webhook not found: {name}")
    ok, status, err = _post(h["url"], {"event": "ping"})
    h["last_sent"] = _now_iso()
    h["last_status"] = status
    _save(hooks)
    return {"name": name, "ok": ok, "status": status, "error": err}


def stats() -> dict:
    """Registry summary: total, enabled, per-mode, last delivery statuses."""
    hooks = _load()
    per_mode = {}
    for h in hooks:
        md = h.get("mode", "shared")
        per_mode[md] = per_mode.get(md, 0) + 1
    recent = sorted(
        (h for h in hooks if h.get("last_sent")),
        key=lambda x: x.get("last_sent") or "",
        reverse=True,
    )[:5]
    return {
        "total": len(hooks),
        "enabled": sum(1 for h in hooks if h.get("enabled")),
        "per_mode": per_mode,
        "recent": [{"name": h["name"], "status": h.get("last_status"),
                    "sent": h.get("last_sent")} for h in recent],
    }


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args:
        for h in list_webhooks():
            state = "on" if h.get("enabled") else "off"
            print(f"  [{state}] {h['name']:<20} {h['url']}  events={','.join(h.get('events') or []) or '-'}")
    elif args[0] == "ping" and len(args) == 2:
        print(json.dumps(ping(args[1]), ensure_ascii=False))
    elif args[0] == "trigger" and len(args) >= 2:
        print(json.dumps(trigger(args[1]), ensure_ascii=False))
