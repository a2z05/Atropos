#!/usr/bin/env python3
"""Atropos links — one-shot share links, stdlib only.

A share link is a random token that grants a single viewer access to a
chat session. Links live in detect.atropos_home()/links.json as::

    {token_hash: {session_id, kind: "chat", created, expires, used}}

Only the SHA-256 of each token is stored, so the registry never contains
usable secrets. ``verify`` consumes the link: the second verification of
the same token fails (one-use enforced).
"""
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from . import detect, settings

KIND = "chat"


def links_path() -> Path:
    """Location of the share-link registry file."""
    return detect.atropos_home() / "links.json"


def _load() -> dict:
    try:
        data = json.loads(links_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict):
    links_path().parent.mkdir(parents=True, exist_ok=True)
    links_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create(session_id: str, ttl_hours: int | None = None) -> dict:
    """Create a one-shot share link for a chat session.

    ttl_hours defaults to settings ``links.ttl_hours`` (default 1). The
    returned dict has {url, token, expires} — the raw token is shown once
    and never stored.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    if ttl_hours is None:
        try:
            ttl_hours = int(settings.get("links.ttl_hours", 1) or 1)
        except Exception:
            ttl_hours = 1
    ttl_hours = max(1, int(ttl_hours))

    token = secrets.token_urlsafe(12)
    now = time.time()
    entry = {
        "session_id": session_id,
        "kind": KIND,
        "created": now,
        "expires": now + ttl_hours * 3600,
        "used": False,
    }
    data = _load()
    data[_hash(token)] = entry
    _save(data)
    return {
        "url": f"/chat?share={token}",
        "token": token,
        "expires": entry["expires"],
        "created": _ts(),
    }


def _entry_for(token: str) -> dict | None:
    return _load().get(_hash(token))


def verify(token: str) -> dict:
    """Verify + consume a share token (one-use, expiry enforced).

    Returns {ok: True, session_id} when the link is valid and unused, or
    {ok: False, error: "…"} otherwise. A successful verify marks the link
    used — a second verify with the same token fails.
    """
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "missing token"}
    data = _load()
    key = _hash(token)
    entry = data.get(key)
    if entry is None:
        return {"ok": False, "error": "invalid or revoked link"}
    if entry.get("used"):
        return {"ok": False, "error": "link already used"}
    if entry.get("expires", 0) < time.time():
        return {"ok": False, "error": "link expired"}
    entry["used"] = True
    _save(data)
    return {"ok": True, "session_id": entry.get("session_id", "")}


def revoke(token: str) -> dict:
    """Revoke a share token by its raw value. Returns {ok, error}."""
    data = _load()
    key = _hash(token)
    if key in data:
        del data[key]
        _save(data)
        return {"ok": True}
    return {"ok": False, "error": "link not found"}


def list_links(active_only: bool = False) -> list:
    """List stored links (ids + metadata, never raw tokens).

    Each row: {id (token hash prefix), session_id, kind, created, expires,
    used, expired}. With active_only=True, only unused, unexpired links
    are returned.
    """
    now = time.time()
    rows = []
    for key, entry in _load().items():
        expired = entry.get("expires", 0) < now
        if active_only and (entry.get("used") or expired):
            continue
        rows.append({
            "id": key[:12],
            "session_id": entry.get("session_id", ""),
            "kind": entry.get("kind", KIND),
            "created": entry.get("created"),
            "expires": entry.get("expires"),
            "used": bool(entry.get("used")),
            "expired": expired,
        })
    return sorted(rows, key=lambda r: r["created"])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "create":
        print(json.dumps(create(sys.argv[2]), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "verify":
        print(json.dumps(verify(sys.argv[2]), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "revoke":
        print(json.dumps(revoke(sys.argv[2]), indent=2, ensure_ascii=False))
    else:
        for row in list_links():
            print(row)
