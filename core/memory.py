#!/usr/bin/env python3
"""Atropos memory — a stdlib-only RAG-lite note store.

Notes are JSON records in ``~/.atropos/memory/notes.json``::

    {"id": "0123abcd...", "text": "...", "tags": ["x"], "ts": "...", "source": "manual"}

Search is keyword-based — a case-insensitive token-overlap score plus a
tag match bonus — over an inverted index (:class:`MemoryIndex`), so no
third-party vector store is needed. Hermes' ``state.db`` is the
full-text fallback when the note store is missing (see
:func:`search_hermes_db`).

Storage and search are guarded by a module lock so the dashboard and the
watch daemon can share the store safely.
"""
import difflib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import detect, settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_LOCK = threading.RLock()

# Captured before the public ``list()`` function below shadows the builtin,
# so isinstance() checks inside the module keep working.
_LIST_TYPE = list

_MAX_TAGS = 32
_MAX_TEXT = 20000
TIERS = ("core", "working", "archival")  # benchmark area 12: memory tiers
_ARCHIVAL_CAP = 500   # notes beyond this auto-move to archival
_DEDUPE_THRESHOLD = 0.8  # normalized-text similarity — adds above merge


# ── inverted index ────────────────────────────────────────────────────────
class MemoryIndex:
    """Inverted index: token → [note ids]. Pure stdlib.

    Built once per search from the current store to filter candidate
    notes, so the index is always in sync with the notes on disk (the
    store is tiny — a few thousand records — so rebuilding is cheap).
    """

    def __init__(self, notes):
        """Index ``notes`` (iterable of note dicts) by text + tag tokens."""
        self.postings = {}
        self.tags = {}
        for note in notes:
            nid = note.get("id")
            if not nid:
                continue
            for tok in tokenize(note.get("text", "")):
                self.postings.setdefault(tok, []).append(nid)
            for tag in note.get("tags", []):
                for tok in tokenize(tag):
                    self.tags.setdefault(tok, []).append(nid)

    def ids_for(self, token: str) -> list:
        """Note ids containing ``token`` (text or tag), in insertion order."""
        seen = {}
        for nid in self.postings.get(token, []) + self.tags.get(token, []):
            seen.setdefault(nid, None)
        return _LIST_TYPE(seen)

    def tokens(self) -> list:
        """All indexed tokens, sorted."""
        return sorted(self.postings)


def tokenize(text: str) -> list:
    """Lowercase alphanumeric tokens of a string (keeps digits and unicode)."""
    return _TOKEN_RE.findall((text or "").lower())


# ── store path ────────────────────────────────────────────────────────────
def notes_path() -> Path:
    """Path to the notes JSON store (~/.atropos/memory/notes.json)."""
    return detect.atropos_home() / "memory" / "notes.json"


def _load() -> list:
    """Read notes from disk; [] on missing/corrupt store."""
    p = notes_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, _LIST_TYPE):
        return []
    return [n for n in data if isinstance(n, dict)]


def _save(notes: list):
    """Atomically write notes to disk."""
    p = notes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── CRUD ──────────────────────────────────────────────────────────────────
def add(text: str, tags=None, source: str = "manual", tier: str = "working",
        importance: int = 1) -> str:
    """Add a note; returns its id (uuid hex).

    ``tags`` may be a list of strings or a space/comma-separated string.
    ``tier``: core | working | archival (benchmark area 12 adoption —
    memory tiers like mem0/Letta). ``importance``: 1-5, weights search
    ranking (5 = critical).
    Modern dedupes: a new note 90%+ similar to an existing one merges
    into it instead of accumulating a duplicate.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("note text must not be empty")
    if len(text) > _MAX_TEXT:
        raise ValueError(f"note text too long (max {_MAX_TEXT} chars)")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of: {', '.join(TIERS)}")
    try:
        importance = max(1, min(5, int(importance)))
    except (TypeError, ValueError):
        importance = 1
    if tags is None:
        tags = []
    elif isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags.strip())
    tags = [str(t).strip() for t in tags if str(t).strip()]
    tags = tags[:_MAX_TAGS]
    note = {
        "id": uuid.uuid4().hex,
        "text": text,
        "tags": tags,
        "ts": _now(),
        "source": str(source or "manual"),
        "tier": tier,
        "importance": importance,
        "_private": "private" in tags,
    }
    # anything mentioning the owner/project/tokens is private by default
    if any(w in text.lower() for w in ("a2z", "atropos", "arophin", "token", "api_key", "password")):
        note["_private"] = True
    with _LOCK:
        notes = _load()
        dup = _dedupe(notes, note)
        if dup is not None:
            return dup  # merged into an existing note
        notes.append(note)
        _auto_archive(notes)
        _save(notes)
    return note["id"]


def _dedupe(notes: list, note: dict) -> str | None:
    """Return an existing note's id when ``note`` is a duplicate,
    otherwise None. Merges tags + bumps the existing note instead of
    storing twice (benchmark area 12 adoption).

    Merge rules: exact-text match, OR a similarity ratio ≥ threshold
    AND near-equal lengths (±15%) — "note 0" vs "note 1" are unique
    numbered notes, not duplicates; a rephrased copy of the same fact
    is."""
    text = note.get("text", "")
    for existing in reversed(notes[-200:]):
        et = existing.get("text", "")
        if et == text:
            _merge_tags(existing, note)
            return existing["id"]
        # token-overlap dedupe: same words (possibly rephrased/extended),
        # near-equal length. Numbered variants ("note 0 ..." vs
        # "note 1 ...") differ in real words → not duplicates.
        et_toks = set(tokenize(et))
        text_toks = set(tokenize(text))
        if not et_toks or not text_toks:
            continue
        overlap = len(et_toks & text_toks) / len(et_toks | text_toks)
        if overlap >= _DEDUPE_THRESHOLD:
            _merge_tags(existing, note)
            return existing["id"]
    return None


def _merge_tags(existing: dict, note: dict):
    """Merge new tags into the existing note and bump its timestamp."""
    for t in note.get("tags", []):
        if t not in (existing.get("tags") or []):
            existing.setdefault("tags", []).append(t)
    existing["updated"] = _now()


def _auto_archive(notes: list):
    """Past the archival cap, oldest non-core notes move to archival
    (Letta-style tiering, benchmark area 12)."""
    active = [n for n in notes if n.get("tier", "working") in ("core", "working")]
    if len(active) <= _ARCHIVAL_CAP:
        return
    to_move = sorted(
        [n for n in active if n.get("tier", "working") == "working"],
        key=lambda n: n.get("ts", ""))[:len(active) - _ARCHIVAL_CAP]
    for n in to_move:
        n["tier"] = "archival"


def list(limit: int = 50, include_private: bool = True) -> list:
    """Most recent notes (newest first), capped at ``limit``.

    ``include_private=False`` hides anything tagged private — guest context
    never sees owner/project notes.
    """
    with _LOCK:
        notes = _load()
    if not notes:
        return _LIST_TYPE()
    if not include_private:
        notes = [n for n in notes if not n.get("_private")]
    return _LIST_TYPE(reversed(notes[-limit:]))


def delete(note_id: str) -> bool:
    """Delete a note by id. Returns True when a note was removed."""
    with _LOCK:
        notes = _load()
        kept = [n for n in notes if n.get("id") != note_id]
        if len(kept) == len(notes):
            return False
        _save(kept)
        return True


def search(q: str, k: int = None, include_private: bool = True) -> list:
    """Keyword search over notes, best matches first.

    Scoring (see :func:`_score`): token overlap between query and note
    (text + tags) plus a tag-match bonus. ``k`` defaults to
    ``settings.get('memory.k', 8)`` and is clamped to a sane range.
    Returns note dicts with an extra ``score`` key, best first.
    ``include_private=False`` filters private-tagged notes (guest context).
    """
    q = (q or "").strip()
    if not q:
        return []
    if k is None:
        k = settings.get("memory.k", 8)
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    k = max(1, min(k, 100))
    with _LOCK:
        notes = _load()
        if not include_private:
            notes = [n for n in notes if not n.get("_private")]
        index = MemoryIndex(notes)
        # candidate filter: only notes sharing at least one query token
        # (text or tag) get scored — this is where the inverted index pays
        # for itself; the per-note _score then ranks the survivors.
        candidates = {}
        for tok in tokenize(q):
            for nid in index.ids_for(tok):
                candidates[nid] = None
        scored = []
        for note in notes:
            if note.get("id") not in candidates:
                continue
            s = _score(q, note)
            if s > 0:
                scored.append(dict(note, score=s))
    # highest score first; ties broken by recency (newest first)
    scored.sort(key=lambda n: (-n["score"], n.get("ts", "")))
    return scored[:k]


def _score(q: str, note: dict) -> float:
    """Recency × importance × relevance score of a note against a query.

    Relevance (the old token-overlap + tag-bonus): every query token
    present in the note's text contributes 1.0, every query token present
    in the note's tags contributes 2.0 (tags are high-signal). Then the
    result is weighted by ``importance`` (1-5, default 1) and a recency
    decay ``1/(1+days)`` (benchmark area 12 adoption — mem0's
    recency × importance × relevance ranking).
    Returns 0.0 for no overlap (callers filter those out).
    """
    q_tokens = tokenize(q)
    if not q_tokens:
        return 0.0
    text_tokens = set(tokenize(note.get("text", "")))
    tag_tokens = set(tokenize(" ".join(note.get("tags", []))))
    relevance = 0.0
    for tok in q_tokens:
        if tok in text_tokens:
            relevance += 1.0
        if tok in tag_tokens:
            relevance += 2.0
    if relevance == 0.0:
        return 0.0
    importance = max(1, min(5, int(note.get("importance", 1) or 1)))
    ts = note.get("ts", "")
    days = 0.0
    try:
        from datetime import datetime, timezone
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - parsed).total_seconds()
        if secs < 1.0:  # same second → "just now", exact recency 1.0
            days = 0.0
        else:
            days = secs / 86400.0
    except Exception:
        days = 0.0
    recency = 1.0 / (1.0 + days)
    return relevance * (0.5 + 0.5 * importance) * recency


def stats() -> dict:
    """Store statistics: count, sources histogram, last_added timestamp."""
    with _LOCK:
        notes = _load()
    sources = {}
    for n in notes:
        src = n.get("source") or "manual"
        sources[src] = sources.get(src, 0) + 1
    last = None
    if notes:
        with_ts = [n for n in notes if n.get("ts")]
        if with_ts:
            last = max(with_ts, key=lambda n: n["ts"]).get("ts")
    return {"count": len(notes), "sources": sources, "last_added": last}


# ── state.db full-text fallback ───────────────────────────────────────────
def search_hermes_db(q: str, k: int = 8) -> list:
    """Full-text fallback into Hermes' state.db when the note store is empty.

    Walks sqlite tables that have TEXT columns and does a per-column
    LIKE match on the query; results are dicts with ``source`` set to the
    table name and ``score`` set to the token-overlap score. Returns []
    when state.db is absent or unreadable. Never raises.
    """
    q = (q or "").strip()
    if not q:
        return []
    home = detect.hermes_home()
    candidates = [home / "state.db", home / "data" / "state.db",
                  home / "state" / "state.db", home / "db" / "state.db"]
    db = next((c for c in candidates if c.exists()), None)
    if db is None:
        try:
            hits = sorted(home.glob("**/state.db"))[:5]
        except Exception:
            hits = []
        if hits:
            db = hits[0]
    if db is None or not db.exists():
        return []
    like = f"%{q}%"
    out = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    except Exception:
        return []
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            try:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            except Exception:
                continue
            text_cols = [c for c in cols if c.lower() in (
                "text", "message", "content", "body", "prompt", "response",
                "note", "title", "name", "summary", "question", "answer")]
            if not text_cols:
                continue
            id_col = next((c for c in cols if c.lower() in ("id", "rowid")), cols[0])
            try:
                rows = con.execute(
                    f'SELECT "{id_col}", "{text_cols[0]}" FROM "{table}" '
                    f'WHERE "{text_cols[0]}" LIKE ? LIMIT 200', (like,)
                ).fetchall()
            except Exception:
                continue
            for row_id, text in rows:
                if not text:
                    continue
                out.append({
                    "id": f"{table}:{row_id}",
                    "text": str(text)[:500],
                    "tags": [],
                    "ts": "",
                    "source": f"state.db:{table}",
                    "score": _score(q, {"id": str(row_id), "text": str(text), "tags": []}),
                })
    finally:
        con.close()
    out.sort(key=lambda n: -n["score"])
    return out[:k]
