#!/usr/bin/env python3
"""Atropos mobile chat engine — sessions, LLM sends, streaming, export.

Sessions persist in a dedicated sqlite database at
``detect.atropos_home()/chat.db`` (tables ``sessions`` and ``messages``).
Hermes' own state.db is never touched.

Sends route through ``core.routing.dispatch()`` to pick the harness
(clotho / lachesis / atropos), then call the active router's
``/chat/completions`` endpoint via ``send_llm`` (same endpoint logic as
``core.router.ping``). Messages starting with ``/`` are console commands
and run through the whitelist in ``core.console`` — the reply is the
captured output lines, and the whitelist itself is the only thing that
decides what may run.

Thread-safety: every public function opens a fresh connection per call
and commits before closing, so dashboard workers and the CLI can use
this module concurrently.
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import console, detect, router, routing, settings

MAX_CONTEXT_MESSAGES = 10   # prior messages sent to the LLM as context
MAX_CONTENT_CHARS = 200_000  # per-message storage cap
REQUEST_TIMEOUT = 60        # seconds for one LLM completion


# ── database ──────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    harness       TEXT NOT NULL DEFAULT 'auto',
    model         TEXT NOT NULL DEFAULT '',
    effort        TEXT NOT NULL DEFAULT 'medium',
    message_count INTEGER NOT NULL DEFAULT 0,
    tags          TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    harness    TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    effort     TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER,
    ts         TEXT NOT NULL,
    tokens     INTEGER
);
"""


def db_path() -> Path:
    """Chat database location: ``~/.atropos/chat.db``."""
    return detect.atropos_home() / "chat.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _init_db():
    """Create the schema on demand (idempotent)."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # migrate older chat.db files: add the tags column when absent
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "tags" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
        # FTS5 index over messages (best-effort; falls back to LIKE at search time)
        from . import search
        search.init_fts(conn)
        conn.commit()
    finally:
        conn.close()


def tag_session(session_id: str, tag: str) -> bool:
    """Add a tag to a session (comma-joined). Returns True when changed."""
    _init_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT tags FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return False
        tags = [t for t in (row["tags"] or "").split(",") if t]
        if tag not in tags:
            tags.append(tag)
            conn.execute("UPDATE sessions SET tags = ? WHERE id = ?",
                         (",".join(tags), session_id))
            conn.commit()
            return True
        return False
    finally:
        conn.close()


def remove_tag(session_id: str, tag: str) -> bool:
    """Remove a tag from a session. Returns True when changed."""
    _init_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT tags FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return False
        tags = [t for t in (row["tags"] or "").split(",") if t and t != tag]
        conn.execute("UPDATE sessions SET tags = ? WHERE id = ?",
                     (",".join(tags), session_id))
        conn.commit()
        return True
    finally:
        conn.close()


def rename_session(session_id: str, title: str) -> str:
    """Rename a session. Returns the stored title (trimmed to 120 chars)."""
    title = (title or "").strip()[:120] or "Chat"
    _init_db()
    conn = _connect()
    try:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        conn.commit()
    finally:
        conn.close()
    return title


def pin_session(session_id: str, pinned: bool = True) -> bool:
    """Pin or unpin a session (pin tag). Returns True when changed."""
    if pinned:
        return tag_session(session_id, "pin")
    return remove_tag(session_id, "pin")


def _now() -> str:
    """UTC ISO-8601 timestamp without sub-second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── sessions ──────────────────────────────────────────────────────────────
def create_session(title: str = "Chat") -> str:
    """Create a session row and return its id (32-char uuid hex)."""
    _init_db()
    sid = uuid.uuid4().hex
    title = (title or "Chat").strip()[:120] or "Chat"
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions (id, title, created, updated, harness, model,"
            " effort, message_count) VALUES (?, ?, ?, ?, 'auto', '', 'medium', 0)",
            (sid, title, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return sid


def search_messages(query: str, k: int = 10) -> list:
    """Full-text search across message content — FTS5, LIKE fallback.

    Ported from Hermes session_search_tool.py / hermes_state_search.py:
    BM25 ranking, snippet rendering, quoted-phrase support, CJK routing.
    """
    _init_db()
    conn = _connect()
    try:
        from . import search
        rows = search.search(conn, query, k=k)
        return [{"session_id": r["session_id"], "title": r["title"],
                 "role": r["role"], "content": r["content"][:300],
                 "snippet": r.get("snippet", ""), "ts": r["ts"]}
                for r in rows]
    finally:
        conn.close()


def session_list(limit: int = 50) -> list:
    """Recent sessions, newest update first.

    Each entry: ``{id, title, created, updated, message_count, harness,
    model, effort, tags}``.
    """
    _init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, title, created, updated, harness, model, effort,"
            " message_count, tags FROM sessions ORDER BY updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
        out.append(d)
    return out


def session_messages(session_id: str, limit: int = 200) -> list:
    """Messages of one session, oldest first.

    Each entry: ``{id, session_id, role, content, harness, model, effort,
    latency_ms, ts, tokens}``.
    """
    _init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, session_id, role, content, harness, model, effort,"
            " latency_ms, ts, tokens FROM messages WHERE session_id = ?"
            " ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_session(session_id: str) -> bool:
    """Delete a session and its messages. Returns True when a session existed."""
    _init_db()
    conn = _connect()
    try:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_message(message_id: int) -> bool:
    """Delete a single message. Returns True when a message existed."""
    _init_db()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM messages WHERE id = ?", (int(message_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def auto_title(text: str) -> str:
    """First 6 words of the opening message, or 'Chat' when empty."""
    words = (text or "").split()
    if not words:
        return "Chat"
    return " ".join(words[:6])


# ── sending ───────────────────────────────────────────────────────────────
def send(session_id=None, text=None, harness=None, effort=None) -> dict:
    """Send one message and persist the exchange.

    * ``text`` starting with ``/`` runs through the console whitelist
      (``core.console.run_command``); the output lines become the reply.
    * anything else is routed via ``core.routing.dispatch(text)`` to a
      harness, then sent to the active router with ``send_llm``.

    A missing ``session_id`` auto-creates a session titled with
    ``auto_title(text)``. ``harness``/``effort`` override routing/chat
    defaults. On transport failure the user message plus a ``system``
    error message are still persisted and ``ok`` is False.

    Returns ``{ok, session_id, reply, harness, model, effort, latency_ms}``
    plus ``error`` when the request failed.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    _init_db()
    if session_id and not _session_exists(session_id):
        return {"ok": False, "error": f"unknown session: {session_id}"}
    if session_id is None:
        session_id = create_session(auto_title(text))
    if effort is None:
        effort = settings.get("chat.effort", "medium")

    conn = _connect()
    try:
        if text.startswith("/"):
            result = _run_console(conn, session_id, text, effort)
        else:
            result = _run_llm(conn, session_id, text, harness, effort)
        conn.commit()
    finally:
        conn.close()
    return result


def _session_exists(session_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def session_exists(session_id: str) -> bool:
    """Public existence check (used by the session engine's registry)."""
    if not session_id:
        return False
    _init_db()
    return _session_exists(session_id)


def message_count(session_id: str) -> int:
    """Message count of a session (used by the session engine's depth gates)."""
    _init_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) c FROM messages WHERE session_id = ?",
                           (session_id,)).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


def _insert_message(conn, session_id, role, content, harness, model, effort,
                    latency_ms, ts, tokens=None):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, harness, model,"
        " effort, latency_ms, ts, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content[:MAX_CONTENT_CHARS], harness, model, effort,
         latency_ms, ts, tokens),
    )


def _bump_session(conn, session_id, harness, model, effort, ts):
    conn.execute(
        "UPDATE sessions SET updated = ?, harness = ?, model = ?, effort = ?,"
        " message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?)"
        " WHERE id = ?",
        (ts, harness, model, effort, session_id, session_id),
    )


def _run_console(conn, session_id, text, effort) -> dict:
    """Whitelisted console command path: reply = captured output lines."""
    now = _now()
    _insert_message(conn, session_id, "user", text, "atropos", "console", effort, 0, now)
    res = console.run_command(text[1:])
    ok = bool(res.get("ok"))
    if ok:
        reply = "\n".join(res.get("output") or []) or "done"
    else:
        reply = res.get("error") or "command failed"
    _insert_message(conn, session_id, "assistant", reply, "atropos", "console", effort, 0, now)
    _bump_session(conn, session_id, "atropos", "console", effort, now)
    return {
        "ok": ok,
        "session_id": session_id,
        "reply": reply,
        "harness": "atropos",
        "model": "console",
        "effort": effort,
        "latency_ms": 0,
        "error": None if ok else (res.get("error") or "command failed"),
    }


def _run_llm(conn, session_id, text, harness, effort) -> dict:
    """LLM path: route, build context, call the active router, persist."""
    # resolve the harness before touching the DB (routing never raises)
    if harness:
        try:
            harness = routing.normalize(harness)
        except ValueError:
            harness = routing.dispatch(text)["harness"]
    else:
        harness = routing.dispatch(text)["harness"]

    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ?"
        " AND role IN ('user', 'assistant') AND content != ''"
        " ORDER BY id DESC LIMIT ?",
        (session_id, MAX_CONTEXT_MESSAGES),
    ).fetchall()
    context = [
        {"role": r["role"], "content": r["content"]} for r in reversed(rows)
    ]

    now = _now()
    _insert_message(conn, session_id, "user", text, harness, "", effort, 0, now)
    context.append({"role": "user", "content": text})

    active = router.get().get("active", "nain")
    res = send_llm(context, active)
    latency = res.get("latency_ms", 0)
    model = res.get("model", router.get().get("model", ""))
    tokens = res.get("tokens")

    if res.get("ok"):
        reply = res["reply"]
        _insert_message(conn, session_id, "assistant", reply, harness, model,
                        effort, latency, now, tokens=tokens)
        _bump_session(conn, session_id, harness, model, effort, now)
        return {
            "ok": True,
            "session_id": session_id,
            "reply": reply,
            "harness": harness,
            "model": model,
            "effort": effort,
            "latency_ms": latency,
            "tokens": tokens,
        }

    error = res.get("error", "LLM request failed")
    _insert_message(conn, session_id, "system", error, harness, model,
                    effort, latency, now, tokens=tokens)
    _bump_session(conn, session_id, harness, model, effort, now)
    return {
        "ok": False,
        "session_id": session_id,
        "reply": "",
        "harness": harness,
        "model": model,
        "effort": effort,
        "latency_ms": latency,
        "tokens": None,
        "error": error,
    }


def send_llm(messages, router_name=None) -> dict:
    """POST a completion request to a router's ``/chat/completions``.

    ``messages`` is the OpenAI-style ``[{role, content}, ...]`` list.
    ``router_name`` defaults to the active router. Endpoint and auth
    mirror ``core.router.ping``: ``{base_url}/chat/completions`` with
    ``model``, ``messages``, ``max_tokens: 256`` and a ``Bearer`` header
    from the router's ``api_key_env`` env var (Ollama needs no key).

    Returns ``{ok, reply, model, latency_ms}`` on success, or
    ``{ok: False, error, latency_ms}`` on any transport/parse failure —
    never raises.
    """
    if router_name is None:
        router_name = router.get().get("active", "nain")
    if router_name not in router.ROUTERS:
        return {"ok": False, "error": f"unknown router: {router_name}", "latency_ms": 0}
    rinfo = router.ROUTERS[router_name]
    endpoint = _endpoint(router_name)
    api_key_env = rinfo["api_key_env"]
    # Check settings-stored keys first, then env vars
    api_key = ""
    try:
        from . import settings as _st
        if router_name == "nain":
            api_key = _st.get("router.ninerouter_key") or ""
        elif router_name == "omni":
            api_key = _st.get("router.openai_key") or ""
        if not api_key:
            api_key = _st.get("router.api_key") or ""
    except Exception:
        pass
    if not api_key:
        api_key = os.environ.get(api_key_env, "")
    headers = {"Content-Type": "application/json"}
    if api_key and api_key_env != "OLLAMA_HOST":
        headers["Authorization"] = f"Bearer {api_key}"
    payload = json.dumps({
        "model": rinfo["model"],
        "messages": messages,
        "max_tokens": 256,
        "stream": False,
    }).encode("utf-8")
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        latency = round((time.monotonic() - t0) * 1000)
        data = json.loads(body)
        reply = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return {
            "ok": True,
            "reply": reply,
            "model": data.get("model", rinfo["model"]),
            "latency_ms": latency,
            "tokens": usage.get("total_tokens"),
        }
    except urllib.error.HTTPError as e:
        latency = round((time.monotonic() - t0) * 1000)
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}", "latency_ms": latency}
    except Exception as e:
        latency = round((time.monotonic() - t0) * 1000)
        return {"ok": False, "error": str(e), "latency_ms": latency}


def _endpoint(router_name: str) -> str:
    """``/chat/completions`` URL for a router (mirrors ``core.router.ping``)."""
    return router._endpoint(router_name, "chat/completions")


# ── streaming / export / stats ────────────────────────────────────────────
def chat_stream(session_id, text, harness=None):
    """Stream one reply as SSE-style dicts: a single delta, then done.

    Yields ``{"event": "delta", "text": ...}`` followed by
    ``{"event": "done", "session_id": ..., "ok": ...}``. The reply is
    produced synchronously — an honest single-chunk stream that the
    frontend renders exactly like a non-streaming send. Never raises.
    """
    res = send(session_id, text, harness=harness)
    if res.get("ok"):
        reply = res["reply"]
    else:
        reply = res.get("error") or "send failed"
    yield {"event": "delta", "text": reply}
    yield {"event": "done", "session_id": res.get("session_id"), "ok": bool(res.get("ok"))}


def export(session_id: str) -> str:
    """Export a session as JSON-lines: one message object per line."""
    msgs = session_messages(session_id)
    return "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)


def stats() -> dict:
    """Session/message counts plus the database path."""
    _init_db()
    conn = _connect()
    try:
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return {"sessions": sessions, "messages": messages, "db": str(db_path())}
