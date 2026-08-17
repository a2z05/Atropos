#!/usr/bin/env python3
"""Atropos session search — FTS5 full-text over the chat database.

Ported from Hermes source with fidelity:
  - hermes-agent/hermes_state_search.py  (query routing, sanitizer, CJK
    handling, snippet rendering)
  - hermes-agent/hermes_state_common.py  (FTS5 virtual-table DDL + triggers)

Deliberate deviations (documented, tested in test_copy_parity):
  - Hermes indexes hermes state.db (sessions/messages with source, active,
    compacted columns); Atropos indexes its own chat.db (sessions/messages
    with harness/model/effort). The FTS table keeps the same external-
    content shape (content='messages', content_rowid='id').
  - Hermes has three indexes (unicode61 + trigram + cjk-bigram); Atropos
    keeps the unicode61 index plus a LIKE fallback for short CJK queries —
    no loadable tokenizers, stdlib-only.
  - Atropos messages have no active/compacted columns, so the discover
    predicate is simply role NOT IN ('system', 'tool').
"""
import re
import sqlite3
from typing import Optional

# ── FTS5 schema (external content over the messages table, triggers kept) ──
# Source: hermes-agent/hermes_state_common.py FTS_SQL (adapted: no
# rebuild-watermark gates — chat.db is append-mostly and small).
FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages
BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages
WHEN old.content IS NOT new.content
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

# ── query sanitizer — FTS5 has its own syntax; user input must not crash ──
# Source: hermes-agent/hermes_state_search.py _sanitize_fts5_query.
_MAX_QUERY_CHARS = 200


def _sanitize_fts5_query(query: str) -> str:
    """Strip FTS5-special characters from user input, keep quoted phrases."""
    query = query[: _MAX_QUERY_CHARS]
    quoted: list[str] = []
    pieces: list[str] = []
    i = 0
    while i < len(query):
        ch = query[i]
        if ch != '"':
            pieces.append(ch)
            i += 1
            continue
        end = query.find('"', i + 1)
        if end == -1:
            pieces.append(" ")
            i += 1
            continue
        quoted.append(query[i : end + 1])
        pieces.append(f"\x00Q{len(quoted) - 1}\x00")
        i = end + 1
    sanitized = "".join(pieces)
    sanitized = re.sub(r'[+{}():\"^]', " ", sanitized)
    sanitized = re.sub(r"\*+", "*", sanitized)
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
    for i, q in enumerate(quoted):
        sanitized = sanitized.replace(f"\x00Q{i}\x00", q)
    return sanitized.strip()


# ── CJK handling — unicode61 splits CJK per-character, breaking phrases ──
# Source: hermes-agent/hermes_state_search.py _contains_cjk/_count_cjk.
def _count_cjk(text: str) -> int:
    return sum(
        1
        for ch in text
        if (
            0x4E00 <= ord(ch) <= 0x9FFF
            or 0x3400 <= ord(ch) <= 0x4DBF
            or 0x20000 <= ord(ch) <= 0x2A6DF
            or 0x3000 <= ord(ch) <= 0x303F
            or 0x3040 <= ord(ch) <= 0x309F
            or 0x30A0 <= ord(ch) <= 0x30FF
            or 0xAC00 <= ord(ch) <= 0xD7AF
        )
    )


def _contains_cjk(text: str) -> bool:
    return _count_cjk(text) > 0


def _fts_available(conn: sqlite3.Connection) -> bool:
    """True when the host SQLite build has FTS5 (external-content probes)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


# ── search ────────────────────────────────────────────────────────────────
def search(conn: sqlite3.Connection, query: str, k: int = 20,
           role_filter: Optional[list] = None) -> list:
    """FTS5 search over chat messages; falls back to LIKE when FTS5 is
    unavailable or the query is a short CJK run (same routing as Hermes).

    Returns rows: ``{id, session_id, role, snippet, content, ts, title}``
    ordered by BM25 rank (or ts DESC on the LIKE path).
    """
    query = (query or "").strip()
    if not query:
        return []
    sanitized = _sanitize_fts5_query(query)
    if not sanitized:
        return []

    roles = role_filter or ["user", "assistant"]
    role_ph = ",".join("?" for _ in roles)

    # Short CJK (1-2 chars) can't match the unicode61 tokenizer — LIKE route.
    if _contains_cjk(sanitized) and _count_cjk(sanitized) < 3:
        return _like_fallback(conn, query, k, roles)

    if not _fts_available(conn):
        return _like_fallback(conn, query, k, roles)

    try:
        rows = conn.execute(
            f"""
            SELECT m.id, m.session_id, m.role,
                   snippet(messages_fts, -1, '>>>', '<<<', '...', 40) AS snip,
                   m.content, m.ts, s.title
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
              AND m.role IN ({role_ph})
            ORDER BY rank
            LIMIT ?
            """,
            [sanitized, *roles, k],
        ).fetchall()
    except sqlite3.OperationalError:
        # Bad query syntax survived the sanitizer (e.g. lone `*`) — LIKE.
        return _like_fallback(conn, query, k, roles)

    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = d.pop("snip") or (d["content"] or "")[:120]
        d["content"] = (d["content"] or "")[:300]
        out.append(d)
    return out


def _like_fallback(conn: sqlite3.Connection, query: str, k: int, roles: list) -> list:
    """LIKE substring scan — same shape as the FTS5 result rows."""
    role_ph = ",".join("?" for _ in roles)
    rows = conn.execute(
        f"""
        SELECT m.id, m.session_id, m.role, m.content, m.ts, s.title
        FROM messages m JOIN sessions s ON s.id = m.session_id
        WHERE m.content LIKE ? AND m.role IN ({role_ph}) AND m.content != ''
        ORDER BY m.id DESC LIMIT ?
        """,
        [f"%{query}%", *roles, k],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = (d["content"] or "")[:120]
        d["content"] = (d["content"] or "")[:300]
        out.append(d)
    return out


# ── anchored window — FTS hit → ±window + session bookends ────────────────
# Source: hermes-agent/hermes_state_search.py get_anchored_view.
def anchored_view(conn: sqlite3.Connection, session_id: str,
                  around_message_id: int, window: int = 5,
                  bookend: int = 3) -> dict:
    """Window of ±window messages around the anchor plus first/last bookends.

    The anchor is always kept regardless of role; window rows filter to
    user/assistant. Bookends skip tool/system rows and only fill when the
    window doesn't already overlap the head/tail.
    """
    rows = conn.execute(
        "SELECT id, session_id, role, content, harness, ts FROM messages"
        " WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id ASC",
        (session_id, around_message_id - window, around_message_id + window),
    ).fetchall()
    if not rows:
        return {"window": [], "messages_before": 0, "messages_after": 0,
                "bookend_start": [], "bookend_end": []}
    ids = [r["id"] for r in rows]
    window_rows = [
        dict(r) for r in rows
        if r["id"] == around_message_id or r["role"] in ("user", "assistant")
    ]
    wmin, wmax = ids[0], ids[-1]
    bookend_start = [
        dict(r) for r in conn.execute(
            "SELECT id, session_id, role, content, harness, ts FROM messages"
            " WHERE session_id = ? AND id < ? AND role IN ('user','assistant')"
            " AND length(content) > 0 ORDER BY id ASC LIMIT ?",
            (session_id, wmin, bookend),
        ).fetchall()
    ]
    bookend_end = [
        dict(r) for r in conn.execute(
            "SELECT id, session_id, role, content, harness, ts FROM messages"
            " WHERE session_id = ? AND id > ? AND role IN ('user','assistant')"
            " AND length(content) > 0 ORDER BY id DESC LIMIT ?",
            (session_id, wmax, bookend),
        ).fetchall()
    ][::-1]
    before = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id < ?",
        (session_id, wmin),
    ).fetchone()[0]
    after = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND id > ?",
        (session_id, wmax),
    ).fetchone()[0]
    return {"window": window_rows, "messages_before": before,
            "messages_after": after, "bookend_start": bookend_start,
            "bookend_end": bookend_end}


def init_fts(conn: sqlite3.Connection) -> bool:
    """Create the FTS5 table + triggers. Returns False when FTS5 is absent."""
    try:
        conn.executescript(FTS_SQL)
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False
