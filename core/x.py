#!/usr/bin/env python3
"""Atropos X/Twitter tools — xurl CLI wrapper.

Ported from the Hermes xurl skill (hermes-agent/skills/social-media/
xurl/SKILL.md — official @xdevplatform/xurl CLI, OAuth 2.0 PKCE with
auto-refresh, JSON on stdout). The read-only xAI ``x_search``
API surface (x_search_tool.py) is not ported because Atropos has no xAI
credentials; the xurl CLI is the official API path.

Every function first checks ``shutil.which("xurl")`` and returns a
graceful {ok: False, error} with a how-to hint when it is absent — same
shape as core/tools.x_post today. Never reads ~/.xurl secrets. Command
shapes from the xurl quick reference:

    xurl post "text"        xurl reply POST_ID "text"
    xurl quote POST_ID ...  xurl delete POST_ID
    xurl read POST_ID       xurl search "QUERY" -n N
    xurl whoami             xurl user @handle
    xurl timeline -n N      xurl mentions -n N
    xurl like/unlike POST_ID  xurl repost/unrepost POST_ID
    xurl bookmark/unbookmark POST_ID  xurl bookmarks -n N  xurl likes -n N
    xurl follow/unfollow @handle  xurl following/followers -n N
    xurl block/unblock @handle  xurl mute/unmute @handle
    xurl dm @handle "msg"   xurl dms -n N
    xurl media upload path  xurl media status MEDIA_ID
    xurl auth status        xurl auth apps list

Deliberate deviations: none in behavior — this is a thin subprocess
wrapper exactly like tools.py's _run, extended for the documented xurl
subcommands (search, DM, reply, read, timeline, whoami, auth status).
"""

import json
import shutil
import subprocess
import sys


def _xurl_path():
    """Return the xurl binary path or None (shutil.which)."""
    return shutil.which("xurl")


def _missing(cmd: str) -> dict:
    return {
        "ok": False,
        "error": (
            "xurl CLI not found (official X API tool from @xdevplatform). "
            "Install it, then authenticate once, and retry: "
            "curl -fsSL https://raw.githubusercontent.com/xdevplatform/xurl/main/install.sh | bash "
            "(or: brew install --cask xdevplatform/tap/xurl / npm install -g @xdevplatform/xurl), "
            f"then run: xurl auth apps add <name> --client-id <id> --client-secret <secret> "
            "&& xurl auth oauth2 --app <name> && xurl auth default <name> && xurl auth status "
            f"(needed for: {cmd})"
        ),
    }


def _run(args: list, timeout: int = 90) -> dict:
    """Run one xurl command; returns {"ok", "output"/"data", "error"}.

    xurl prints JSON to stdout; parsed when possible so callers get
    structured data instead of raw text.
    """
    if not _xurl_path():
        return _missing(args[0] if args else "xurl")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"xurl error: {e}"}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "error": stderr or stdout or f"xurl exited {proc.returncode}"}
    data = None
    try:
        data = json.loads(stdout) if stdout else None
    except ValueError:
        pass
    return {"ok": True, "output": stdout, "data": data}


def _have(cmd: str) -> bool:
    """Return True when the xurl CLI is on PATH (cheap probe)."""
    return _xurl_path() is not None


# ── post / reply / quote / delete ──────────────────────────────────────────

def x_post(text: str) -> dict:
    """Post a tweet: xurl post "text"."""
    if not text or not text.strip():
        return {"ok": False, "error": "text is required"}
    return _run(["xurl", "post", text.strip()])


def x_reply(post_id: str, text: str) -> dict:
    """Reply to a post: xurl reply POST_ID "text"."""
    if not post_id or not text or not text.strip():
        return {"ok": False, "error": "post_id and text are required"}
    return _run(["xurl", "reply", post_id, text.strip()])


def x_quote(post_id: str, text: str) -> dict:
    """Quote-tweet a post: xurl quote POST_ID "text"."""
    if not post_id or not text or not text.strip():
        return {"ok": False, "error": "post_id and text are required"}
    return _run(["xurl", "quote", post_id, text.strip()])


def x_delete(post_id: str) -> dict:
    """Delete a post: xurl delete POST_ID."""
    if not post_id:
        return {"ok": False, "error": "post_id is required"}
    return _run(["xurl", "delete", post_id])


# ── read / search ──────────────────────────────────────────────────────────

def x_read(post_id: str) -> dict:
    """Read one post: xurl read POST_ID (URLs are auto-extracted to IDs)."""
    if not post_id:
        return {"ok": False, "error": "post_id is required"}
    return _run(["xurl", "read", post_id])


def x_search(query: str, n: int = 10) -> dict:
    """Search posts: xurl search "QUERY" -n N."""
    if not query or not query.strip():
        return {"ok": False, "error": "query is required"}
    try:
        n = max(1, min(int(n), 100))
    except (TypeError, ValueError):
        n = 10
    return _run(["xurl", "search", query.strip(), "-n", str(n)])


# ── identity / timelines / actions ─────────────────────────────────────────

def x_whoami() -> dict:
    """Show the authenticated account: xurl whoami."""
    return _run(["xurl", "whoami"])


def x_user(handle: str) -> dict:
    """Look up a user: xurl user @handle."""
    if not handle:
        return {"ok": False, "error": "handle is required"}
    return _run(["xurl", "user", handle])


def x_timeline(n: int = 20) -> dict:
    """Home timeline: xurl timeline -n N."""
    try:
        n = max(1, min(int(n), 200))
    except (TypeError, ValueError):
        n = 20
    return _run(["xurl", "timeline", "-n", str(n)])


def x_mentions(n: int = 10) -> dict:
    """Mentions: xurl mentions -n N."""
    try:
        n = max(1, min(int(n), 100))
    except (TypeError, ValueError):
        n = 10
    return _run(["xurl", "mentions", "-n", str(n)])


def x_like(post_id: str) -> dict:
    """Like a post: xurl like POST_ID."""
    if not post_id:
        return {"ok": False, "error": "post_id is required"}
    return _run(["xurl", "like", post_id])


def x_unlike(post_id: str) -> dict:
    """Unlike a post: xurl unlike POST_ID."""
    if not post_id:
        return {"ok": False, "error": "post_id is required"}
    return _run(["xurl", "unlike", post_id])


def x_repost(post_id: str) -> dict:
    """Repost: xurl repost POST_ID."""
    if not post_id:
        return {"ok": False, "error": "post_id is required"}
    return _run(["xurl", "repost", post_id])


# ── DM flows ───────────────────────────────────────────────────────────────

def x_dm(handle: str, message: str) -> dict:
    """Send a DM: xurl dm @handle "message"."""
    handle = (handle or "").strip()
    message = (message or "").strip()
    if not handle or not message:
        return {"ok": False, "error": "handle and message are required"}
    return _run(["xurl", "dm", handle, message])


def x_dms(n: int = 10) -> dict:
    """List recent DMs: xurl dms -n N."""
    try:
        n = max(1, min(int(n), 100))
    except (TypeError, ValueError):
        n = 10
    return _run(["xurl", "dms", "-n", str(n)])


# ── auth ───────────────────────────────────────────────────────────────────

def x_auth_status() -> dict:
    """Credential check: xurl auth status.

    Callers use this (not the ~/.xurl file) to verify setup — the skill
    forbids reading ~/.xurl contents.
    """
    return _run(["xurl", "auth", "status"])


def available() -> bool:
    """Return True when xurl is installed (check_fn equivalent)."""
    return _have("xurl")


if __name__ == "__main__":
    cmd = sys.argv[1:2] or ["status"]
    print(json.dumps(x_auth_status() if cmd[0] == "status" else _missing(cmd[0]), indent=2))