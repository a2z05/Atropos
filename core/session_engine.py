"""Single Session Engine (v19 M) — one session for everything, both modes.

One logical entry for every conversation (Telegram, dashboard chat, CLI
REPL, agents). Per message it decides — with zero added latency in the
common case — which session that message belongs to, and where a topic
change is detected it can *mirror* the exchange into the right session
without ever blocking the reply.

Modes (``settings.session_engine.mode``, overridable per surface via
``settings.session_engine.surfaces.<name>``):
  * ``unified``    — one session per surface; topics stay inside it as
                     lightweight thread markers. Fastest; no routing.
  * ``auto-split`` — a new session per topic, auto-created/resumed.
  * ``hybrid``     — unified base; very-confident new topics split out
                     into their own sub-session once the chat is deep.

Speed guarantee (HARD, v19 M2): the reply ALWAYS starts immediately in
the current session — deep classification is async and may only mirror
the exchange into a better-fitting session afterwards; it never blocks.

Pipeline per message:
    classify (cheap, 0-3ms) → decide (mode + affinity + thresholds)
    → route → mirror (async deep switch, copy-not-move, tagged).

Sessions are Atropos chat sessions (``core.chat.create_session`` /
``session_list`` / ``session_messages``); thread/mirror state rides in
the engine's own ``threads`` / ``message_topics`` / ``mirror_links`` /
``session_meta`` tables inside the chat database.

Surface ids: "telegram" | "dashboard" | "cli" | "agents".
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from . import chat, settings
from . import session_classify as classifier

# ── mode machinery ────────────────────────────────────────────────────────
MODE_CARDS = {
    "unified": {
        "title": "Unified",
        "tagline": "One conversation, neatly organized in threads.",
        "explanation": ("Every message flows into one session per surface. "
                        "Topics are tracked as thread markers — the AI keeps "
                        "them apart automatically and can pull in the right "
                        "context on demand. Fastest mode."),
        "when": "Everyday chat, anything-to-anything.",
        "latency": "0 extra ms.",
        "example": ("\"deploy\" → followed by \"and book a flight\" → the "
                    "second gets a thread marker, same session"),
    },
    "auto-split": {
        "title": "Auto-split",
        "tagline": "A new session is created per topic automatically.",
        "explanation": ("Each new topic gets its own session — the engine "
                        "classifies every message, routes instantly when "
                        "confident, and mirrors the exchange when a deep "
                        "check later disagrees. Old sessions resume by "
                        "topic match."),
        "when": "Lots of distinct projects in one day.",
        "latency": "0 extra ms for ~80% of messages, ≤3ms for the rest; "
                   "the deep check never blocks.",
        "example": ("\"deploy railway\" → routes to the deploy session; "
                    "\"book a flight\" → creates a travel session"),
    },
    "hybrid": {
        "title": "Hybrid",
        "tagline": "Mostly one session; deep new topics start their own.",
        "explanation": ("Unified by default — but when the cheap classifier "
                        "is very confident of a genuinely new topic (and the "
                        "conversation is deep enough), a sub-session is "
                        "created. Everything else stays in the base "
                        "session."),
        "when": "One main conversation plus occasional distinct projects.",
        "latency": "Same as unified in the common case.",
        "example": ("one daily session + a \"research: X\" session that "
                    "splits out when you clearly switch projects"),
    },
}

_DEFAULT_TUNABLES = {
    "classifier": "cheap",          # cheap | deep | hybrid
    "affinity_bias": 0.8,           # stay-in-session strength
    "confidence_threshold": 0.6,    # gate for async deep classifier
    "mirror_on_deep_switch": True,
    "new_topic_min_messages": 3,    # msgs before a session is "locked"
    "session_titles": "auto",       # auto | manual | ask
    "max_sessions": 50,
    "hybrid_confidence": 0.9,       # split gate for hybrid mode
    "hybrid_min_depth": 25,
    "hybrid_max_split_sessions": 6,
}

# ¥ µ-like symbols removed; str.translate only used in classifier.
_THREAD_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{1,40}$", re.IGNORECASE)

# per-surface session id maps (thread-safety: GIL + atomic dict writes)
_current: dict[str, str] = {}      # surface -> chat session id
_threads: dict[str, str] = {}      # surface -> active thread label ("" none)
_sessions_seen: dict[str, int] = {}  # surface -> message count (new-topic lock)

# history for explain/undo + mirror registry (surface, msg role index)
_history: list[dict] = []
_MIRROR_LIMIT = 500


# ── surface / settings ────────────────────────────────────────────────────
def surface_mode(surface: str) -> str:
    """Per-surface mode with global fallback (default unified).

    ``off`` is a per-surface bypass: the engine returns the pass-through
    decision (current session, no classification, no threads).
    """
    mode = settings.get(f"session_engine.surfaces.{surface}", None)
    if mode == "off":
        return "off"
    if mode not in ("unified", "auto-split", "hybrid"):
        mode = settings.get("session_engine.mode", "unified")
    return mode if mode in ("unified", "auto-split", "hybrid") else "unified"


def _t(key: str, default=None):
    """Tunable read: per-surface override, else global, else built-in.

    Override keys live at ``session_engine.surfaces.<key>`` (choice type,
    ``None`` default) so unset overrides fall through cleanly.
    """
    val = settings.get(f"session_engine.surfaces.{key}", None)
    if val is None:
        val = settings.get(f"session_engine.{key}", _DEFAULT_TUNABLES.get(key, default))
    return val


def _matches_current(surface: str, text: str, info: dict) -> bool:
    """True when the new-topic message also matches the current session's
    identity (title/keywords) — then it's the same topic, not a split."""
    cur = _current.get(surface)
    if not cur:
        return False
    meta = _meta_keywords(cur)
    try:
        for s in chat.session_list(limit=5):
            if s.get("id") == cur:
                title = s.get("title", "")
                break
        else:
            title = ""
    except Exception:
        title = ""
    topic_hit = info.get("title") or ""
    if topic_hit and (topic_hit.lower() in title.lower()
                      or topic_hit.lower() in [k.lower() for k in meta]):
        return True
    return classifier.score(text, title, meta) > info.get("confidence", 0)


def _at_session_cap(surface: str) -> bool:
    """True when the surface already has >= max_sessions sessions."""
    cap = int(_t("max_sessions", 50))
    try:
        return len(chat.session_list(limit=500)) >= cap
    except Exception:
        return False


def _oldest_reusable(surface: str) -> str | None:
    """Oldest non-pinned session id (cap reuse target), or None."""
    try:
        for s in chat.session_list(limit=500):
            tags = s.get("tags", "") or ""
            if "pin" in tags.split(","):
                continue
            return s.get("id")
    except Exception:
        return None
    return None


# ── store ─────────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(chat.db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# db path -> True after schema init; keyed by path because tests (and
# home relocation) swap ATROPOS_HOME mid-process
_tables_ready: dict[str, bool] = {}


def _ensure_tables():
    """Idempotent schema init for engine tables inside the chat db.

    The DDL is CREATE IF NOT EXISTS, so one run per db path per process
    is enough; re-running it on every call meant a fresh connection plus
    a full executescript on each engine read/write (review fix)."""
    key = str(chat.db_path())
    if _tables_ready.get(key):
        return
    conn = _conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _tables_ready[key] = True


_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    surface      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    label        TEXT NOT NULL,
    msg_count    INTEGER NOT NULL DEFAULT 0,
    created      TEXT NOT NULL,
    updated      TEXT NOT NULL,
    PRIMARY KEY (surface, session_id, label)
);
CREATE TABLE IF NOT EXISTS message_topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    surface    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    topic      TEXT NOT NULL DEFAULT 'general',
    msg        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    surface       TEXT NOT NULL,
    source_sid    TEXT NOT NULL,
    target_sid    TEXT NOT NULL,
    msg_id        INTEGER NOT NULL,
    mirrored_at   TEXT NOT NULL,
    undone        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY,
    surface    TEXT NOT NULL DEFAULT '',
    keywords   TEXT NOT NULL DEFAULT '',
    topic      TEXT NOT NULL DEFAULT '',
    auto_split INTEGER NOT NULL DEFAULT 0,
    created    TEXT NOT NULL,
    updated    TEXT NOT NULL
);
"""


def _meta_keywords(sid: str) -> list[str]:
    """Keywords learned for a session (dictionary growth, v19 M4)."""
    _ensure_tables()
    conn = _conn()
    try:
        row = conn.execute("SELECT keywords FROM session_meta WHERE session_id = ?",
                           (sid,)).fetchone()
        return (row["keywords"].split(",") if row and row["keywords"] else [])
    finally:
        conn.close()


def _bump_meta(sid: str, surface: str, keywords: list[str], topic: str,
               auto_split: bool):
    """Record/refresh session_meta (title keywords + auto-split flag)."""
    _ensure_tables()
    now = _now()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO session_meta (session_id, surface, keywords, topic,"
            " auto_split, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET keywords = excluded.keywords,"
            " topic = excluded.topic, auto_split = excluded.auto_split,"
            " surface = excluded.surface, updated = excluded.updated",
            (sid, surface, ",".join(keywords), topic, 1 if auto_split else 0,
             now, now))
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── public surface API ────────────────────────────────────────────────────
def classify_message(text: str, surface: str = "cli") -> dict:
    """Full per-message decision (mode + affinity + thresholds).

    This is what adapters call right before sending a message to the
    model. It NEVER blocks on a network call (deep classification is a
    no-op here — the engine mirrors later via :func:`mirror_later`).

    Returns ``{surface, session_id, mode, decision, thread, mirrored,
    latency_ms}`` — ``session_id`` is the session the message SHOULD
    live in (current session in unified/hybrid; possibly a different
    one in auto-split).
    """
    t0 = time.perf_counter()
    mode = surface_mode(surface)
    if mode == "off":
        # per-surface bypass: plain pass-through, zero classification
        return {"surface": surface, "mode": "off",
                "session_id": _current.get(surface) or _ensure_session(surface),
                "thread": "", "decision": "current", "mirrored": False,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3)}
    current = _ensure_session(surface)
    res = {
        "surface": surface, "mode": mode,
        "session_id": current, "thread": _threads.get(surface, "") or "",
        "decision": "current", "mirrored": False,
    }

    if mode == "auto-split":
        res.update(_auto_split_decide(text, surface, current))
    else:
        # unified / hybrid: lightweight thread markers only; hybrid
        # additionally splits a very-confident new topic into its own
        # sub-session once the base session is deep enough (M3)
        info = classifier.classify(text, _session_titles(surface), surface=surface)
        if info["decision"] in ("new", "existing") and info["confidence"] > 0:
            if mode == "hybrid" and info["decision"] == "new" \
                    and _hybrid_should_split(info, surface):
                target = _create_new_session(surface,
                                             info.get("title") or "New topic", text)
                res["session_id"] = target
            else:
                _tick_topic(surface, current, info.get("title") or "general")
        res["thread"] = _threads.get(surface, "") or ""
        if not res.get("session_id"):
            res["session_id"] = current

    res["latency_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    return res


def _hybrid_should_split(info: dict, surface: str = "cli") -> bool:
    """Hybrid split gate: confidence >= hybrid_confidence AND the base
    session is deep enough (>= hybrid_min_depth messages)."""
    conf = float(_t("hybrid_confidence", 0.9))
    depth = int(_t("hybrid_min_depth", 25))
    base = _current.get(surface, "")
    msgs = 0
    try:
        if base and chat.session_exists(base):
            msgs = chat.message_count(base)
    except Exception:
        msgs = 0
    return info.get("confidence", 0) >= conf and msgs >= depth


def _auto_split_decide(text: str, surface: str, current: str) -> dict:
    """Decision pipeline for auto-split (M2): affinity → cheap → async
    deep (non-blocking, mirror later)."""
    info = classifier.classify(text, _session_titles(surface), surface=surface)
    affinity = float(_t("affinity_bias", 0.8))
    threshold = float(_t("confidence_threshold", 0.6))
    decision = info["decision"]
    confidence = info["confidence"]

    # 1. route: a confident target wins outright (existing session or a
    #    fresh auto-split for a confident new topic). A weak existing
    #    match still routes back — the best real session identity beats
    #    the stay-put default for a same-topic follow-up.
    if decision == "existing" and confidence > 0:
        target = info["session_id"]
    elif decision == "new" and confidence >= threshold \
            and _sessions_seen.get(surface, 0) >= int(_t("new_topic_min_messages", 3)):
        if _matches_current(surface, text, info):
            # the topic IS the current session's topic — stay (the
            # classifier excluded the current session from candidates)
            target = current
        elif _at_session_cap(surface):
            # cap reached → reuse the oldest non-pinned session instead
            target = _oldest_reusable(surface) or current
            decision = "current"
        else:
            target = _create_new_session(surface, info.get("title") or "New topic",
                                         text)
    elif decision == "new" and confidence < affinity:
        # 2. affinity: an unclear new topic stays in the current session
        #    (the default common path — zero added latency)
        decision = "current"
        target = current
    else:
        target = current

    _tick_topic(surface, target, info.get("title") or "auto-split")
    return {"session_id": target, "decision": decision,
            "mirrored": False}


def _ensure_session(surface: str) -> str:
    """Return (and lazily create) the current session for a surface."""
    sid = _current.get(surface)
    if sid and chat.session_exists(sid):
        return sid
    sid = chat.create_session(f"Chat - {surface}")
    _current[surface] = sid
    _sessions_seen[surface] = 0
    sid_title = f"Chat - {surface}"  # title auto-updates via chat.auto_title
    try:
        chat.rename_session(sid, sid_title)
    except Exception:
        pass
    return sid


def _create_new_session(surface: str, topic: str, first_message: str = "") -> str:
    """Auto-split: create a brand-new session for a detected topic.

    The seen-counter is NOT reset — a split is part of the same ongoing
    conversation, and resetting it would starve the next split's
    ``new_topic_min_messages`` gate."""
    sid = chat.create_session(f"{topic[:60]} - {surface}")
    _current[surface] = sid
    # learn keywords from the message that triggered the split (dictionary
    # growth, v19 M4) — the topic title alone is too thin to route back on
    keywords = classifier.learn_topic(first_message or topic, topic)
    _bump_meta(sid, surface, keywords, topic, auto_split=True)
    return sid


def _session_titles(surface: str) -> list[dict]:
    """All known sessions minus the current one (avoid self-match)."""
    cur = _current.get(surface)
    out = []
    try:
        sessions = [s for s in chat.session_list(limit=200)
                    if s.get("id") != cur]
        # one connection for every keyword lookup — a per-session
        # _meta_keywords() call here meant 2 connects + DDL per session
        # on the per-message classify path (review fix)
        _ensure_tables()
        conn = _conn()
        try:
            kw: dict[str, list[str]] = {}
            for s in sessions:
                sid = s.get("id")
                row = conn.execute(
                    "SELECT keywords FROM session_meta WHERE session_id = ?",
                    (sid,)).fetchone()
                kw[sid] = (row["keywords"].split(",")
                           if row and row["keywords"] else [])
        finally:
            conn.close()
        for s in sessions:
            out.append({"id": s.get("id"), "title": s.get("title", ""),
                        "keywords": kw.get(s.get("id"), [])})
    except Exception:
        pass
    return out


def _tick_topic(surface: str, sid: str, topic: str):
    """Thread marker + message_topics row + seen counter."""
    _ensure_tables()
    now = _now()
    lab = topic.strip()[:48] or "general"
    _sessions_seen[surface] = _sessions_seen.get(surface, 0) + 1
    # one connection for the whole tick — the thread write and the
    # message_topics insert belong to a single logical transaction
    conn = _conn()
    try:
        if lab != "general" and lab != _threads.get(surface):
            _threads[surface] = lab
            conn.execute(
                "INSERT INTO threads (surface, session_id, label, msg_count,"
                " created, updated) VALUES (?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(surface, session_id, label) DO UPDATE SET"
                " msg_count = msg_count + 1, updated = excluded.updated",
                (surface, sid, lab, now, now))
        elif lab == _threads.get(surface):
            conn.execute(
                "UPDATE threads SET msg_count = msg_count + 1, updated = ?"
                " WHERE surface = ? AND session_id = ? AND label = ?",
                (now, surface, sid, lab))
        conn.execute("INSERT INTO message_topics (surface, session_id, ts, topic, msg)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (surface, sid, now, lab, "·"))
        conn.commit()
    finally:
        conn.close()


def _mirror_message(surface: str, source_sid: str, target_sid: str, topic: str):
    """Copy-not-move: record a mirror link (deep-switch decision)."""
    _ensure_tables()
    now = _now()
    conn = _conn()
    try:
        conn.execute("INSERT INTO mirror_links (surface, source_sid, target_sid,"
                     " msg_id, mirrored_at, undone) VALUES (?, ?, ?, ?, ?, 0)",
                     (surface, source_sid, target_sid, 0, now))
        conn.commit()
    finally:
        conn.close()
    _history.append({"t": "mirror", "surface": surface, "from": source_sid,
                     "to": target_sid, "topic": topic, "at": now})
    if len(_history) > _MIRROR_LIMIT:
        del _history[:len(_history) - _MIRROR_LIMIT]


def mirror_later(text: str, surface: str, ctx: dict | None = None):
    """Async deep-classifier hook — may mirror the exchange into the
    target session after the reply already started. Never blocks."""
    if ctx and ctx.get("decision") in ("current", "none"):
        return
    try:
        info = classifier.classify(text, _session_titles(surface), surface=surface)
        cur = _current.get(surface)
        target = None
        if info["decision"] == "existing" and info["session_id"]:
            target = info["session_id"]
        elif info["decision"] == "new" and info.get("confidence", 0) > 0 \
                and info.get("title"):
            # a confident new topic with no session yet → create the
            # target session for it (copy-not-move; the original stays)
            target = _create_new_session(surface,
                                         info.get("title") or "New topic", text)
        if target and target != cur:
            _mirror_message(surface, cur or "", target,
                            info.get("title") or "")
    except Exception:
        pass


def submit_message(text: str, surface: str = "cli",
                   session_id: str | None = None) -> dict:
    """Record a message + update thread/mirror state. Called by adapters
    AFTER the reply is produced (mirrors are copy-not-move)."""
    _ensure_tables()
    if session_id is None:
        session_id = _current.get(surface)
    now = _now()
    conn = _conn()
    try:
        conn.execute("INSERT INTO message_topics (surface, session_id, ts, topic, msg)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (surface, session_id, now,
                      _threads.get(surface, "") or "general", text[:500]))
        conn.commit()
    finally:
        conn.close()
    _sessions_seen[surface] = _sessions_seen.get(surface, 0) + 1
    return {"ok": True}


def request_context(surface: str, text: str, k: int = 8) -> dict:
    """Context packing for the reply: current thread's recent messages
    (strong weight) followed by a global session summary (weak weight)
    — never the whole session dump (M1)."""
    _ensure_tables()
    sid = _current.get(surface)
    thread = _threads.get(surface, "")
    msgs = []
    try:
        msgs = chat.session_messages(sid, limit=k) if sid else []
    except Exception:
        msgs = []
    return {"session_id": sid, "thread": thread, "messages": msgs}


def switch_session(surface: str, session_id: str) -> dict:
    """Manual route (v19: ``/session route``, manual route button)."""
    _ensure_tables()
    if session_id and chat.session_exists(session_id):
        _current[surface] = session_id
        _threads[surface] = ""
        return {"ok": True, "session_id": session_id}
    return {"ok": False, "error": "no such session"}


def set_thread(surface: str, label: str) -> dict:
    """Manual thread start/end (``/thread <name>`` / ``/end``)."""
    _ensure_tables()
    sid = _current.get(surface)
    if label:
        if _THREAD_TAG_RE.match(label):
            _threads[surface] = label
            return {"ok": True}
        return {"ok": False, "error": "thread names: letters/digits/space/_/- (≤40)"}
    _threads[surface] = ""
    return {"ok": True}


def merge_sessions(surface: str, a: str, b: str) -> dict:
    """Merge two sessions into one — messages of B move into A, B is
    deleted. ``<sessionA> <sessionB>`` per the brief."""
    _ensure_tables()
    if a == b:
        return {"ok": False, "error": "same session"}
    if not (chat.session_exists(a) and chat.session_exists(b)):
        return {"ok": False, "error": "no such session"}
    conn = _conn()
    try:
        conn.execute("UPDATE messages SET session_id = ? WHERE session_id = ?",
                     (a, b))
        conn.execute("UPDATE threads SET session_id = ? WHERE session_id = ?",
                     (a, b))
        conn.execute("UPDATE mirror_links SET target_sid = ? WHERE target_sid = ?",
                     (a, b))
        conn.commit()
    finally:
        conn.close()
    chat.delete_session(b)
    if _current.get(surface) == b:
        _current[surface] = a
    sb = chat.session_list(limit=1)
    return {"ok": True, "merged_into": a}


def pin_session(surface: str, session_id: str, pinned: bool = True) -> dict:
    return {"ok": chat.pin_session(session_id, pinned), "session_id": session_id}


def unmirror(surface: str, mirror_id: int) -> dict:
    """Undo a mirror: mark undone (original stays; copy removed by caller)."""
    _ensure_tables()
    conn = _conn()
    try:
        conn.execute("UPDATE mirror_links SET undone = 1 WHERE id = ?", (mirror_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def stats() -> dict:
    """Engine stats: split/mirror/thread counts, latency buckets."""
    _ensure_tables()
    conn = _conn()
    try:
        splits = conn.execute(
            "SELECT COUNT(*) c FROM session_meta WHERE auto_split = 1").fetchone()["c"]
        mirrors = conn.execute(
            "SELECT COUNT(*) c FROM mirror_links WHERE undone = 0").fetchone()["c"]
        threads = conn.execute("SELECT COUNT(*) c FROM threads").fetchone()["c"]
        topics = conn.execute("SELECT COUNT(*) c FROM message_topics").fetchone()["c"]
    finally:
        conn.close()
    return {
        "mode": settings.get("session_engine.mode", "unified"),
        "surfaces": {s: surface_mode(s) for s in ("telegram", "dashboard", "cli", "agents")},
        "splits": splits, "mirrors": mirrors, "threads": threads, "topics": topics,
        "current": dict(_current), "active_threads": {s: t for s, t in _threads.items() if t},
    }


def explain(message_text: str, surface: str = "cli") -> str:
    """Explain the routing decision trail for a message (``/session
    explain <msg>``)."""
    info = classifier.classify(message_text, _session_titles(surface), surface=surface)
    mode = surface_mode(surface)
    lines = [f"[session] message: {message_text[:60]}",
             f"[session] surface: {surface}  mode: {mode}"]
    lines.append(f"[session] cheap classifier: {info['decision']}"
                 f" ({info.get('title') or '—'}, score {info['score']:.2f},"
                 f" {info['latency_ms']}ms)")
    if mode == "auto-split":
        lines.append(f"[session] affinity bias: {_t('affinity_bias', 0.8)} "
                     f"threshold: {_t('confidence_threshold', 0.6)}")
    else:
        t = _threads.get(surface, "")
        lines.append(f"[session] unified: current session "
                     f"{_current.get(surface, '—')}" + (f", thread \"{t}\"" if t else ""))
    return "\n".join(lines)


def reset_surface(surface: str):
    """Test/panel helper: forget current session + thread for a surface."""
    _current.pop(surface, None)
    _threads.pop(surface, None)
    _sessions_seen.pop(surface, None)


# ── session listing helper (used by CLI/telegram/dashboard) ───────────────
def sessions_detailed(surface: str | None = None, limit: int = 50) -> list[dict]:
    """Sessions with engine decorations: keywords, thread chips, mirror
    badges, auto-split flag."""
    _ensure_tables()
    sessions = chat.session_list(limit=limit)
    # one connection for the whole listing — a connect per session made
    # the panel refresh pay N round-trips (review fix)
    conn = _conn()
    try:
        out = []
        for s in sessions:
            sid = s.get("id", "")
            meta = conn.execute("SELECT keywords, topic, auto_split FROM session_meta"
                                " WHERE session_id = ?", (sid,)).fetchone()
            trows = conn.execute("SELECT label, msg_count FROM threads WHERE session_id = ?",
                                 (sid,)).fetchall()
            mrows = conn.execute("SELECT id, source_sid, target_sid, undone"
                                 " FROM mirror_links WHERE source_sid = ? OR target_sid = ?",
                                 (sid, sid)).fetchall()
            entry = dict(s)
            entry.update({
                "keywords": (meta["keywords"].split(",") if meta and meta["keywords"] else []),
                "topic": (meta["topic"] if meta else ""),
                "auto_split": bool(meta and meta["auto_split"]),
                "threads": [{"label": r["label"], "msg_count": r["msg_count"]} for r in trows],
                "mirrors": [{"id": r["id"], "source_sid": r["source_sid"],
                             "target_sid": r["target_sid"], "undone": bool(r["undone"])}
                            for r in mrows],
            })
            out.append(entry)
    finally:
        conn.close()
    return out