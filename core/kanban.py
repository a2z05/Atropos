#!/usr/bin/env python3
"""Kanban board — card board with Hermes move semantics.

Ported from hermes-agent/tools/kanban_tools.py (+ hermes_cli/kanban_db.py).

Atropos keeps the original simple JSON store (``~/.atropos/kanban.json``,
a ``{col: [card, ...]}`` map — the tools.py kanban shim and the ``atropos
kanban`` CLI already read that shape), but adopts the real move semantics
from the Hermes source instead of the shim's text-match move:

  - Cards carry an id (``t_<8 hex>`` — same scheme as Hermes'
    ``_new_task_id``) so moves are id-exact, not text-exact.
  - Columns keep the source statuses (``todo``, ``doing``, ``done``) and
    the source status list ordering (``VALID_STATUSES`` order), matching
    Hermes' `status ASC` column sort.
  - ``move`` returns the card's final status, mirroring how
    ``kanban_block`` reports ``status`` after the transition landed
    (and how ``_handle_complete``/``_handle_create`` echo status back).

Deviations (deliberate, documented under constraints):
  - Hermes is a SQLite task engine (states: triage/todo/scheduled/ready/
    running/blocked/review/done/archived, claims, runs, comments, events);
    Atropos is stdlib-only and the shim is JSON. We keep the JSON store.
  - Card ids are ``t_`` + 4 hex bytes exactly like Hermes
    ``_new_task_id()``; dead cards are never reused (source ids never
    mutate either).
  - No priority/assignee/dependency machinery — the JSON card schema is
    ``{id, text, ts}`` (plus ``notes`` via ``{**(card)}`` forwards, so
    Hermes-style metadata passes through).
  - ``add`` does not check for an identical existing card (Hermes
    ``create_task`` treats a duplicate title as a new task; only its
    ``idempotency_key`` path dedupes, and that key has no JSON analog).
"""
import json
import secrets
import time
from pathlib import Path

from . import detect

_list = list  # module-level api list() shadows the builtin — alias it

_COLS = ("todo", "doing", "done")  # Hermes VALID_STATUSES column order (subset)


def _path() -> Path:
    return detect.atropos_home() / "kanban.json"


def board() -> dict:
    """Read the whole board: ok, cols (in source column order), id -> card."""
    cols = {name: [] for name in _COLS}
    p = _path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        for name, cards in data.items():
            if name in cols and isinstance(cards, _list):
                cols[name] = _list(cards)
    index = {}
    for name in _COLS:
        for card in cols[name]:
            if isinstance(card, dict) and card.get("id"):
                index[card["id"]] = {"col": name, "card": card}
    return {"ok": True, "cols": cols, "index": index}


def _save(cols: dict) -> dict:
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(cols, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"kanban: cannot write {_path()}: {e}"}
    return board()


def _status_of(col: str) -> str:
    """Card's source status for a column: done -> done, else col name."""
    if col == "done":
        return "done"
    return col  # doing -> running in Hermes terms, keep the JSON col name


def _new_task_id() -> str:
    # Same id scheme as hermes_cli/kanban_db.py _new_task_id(): 4 hex bytes
    # of cryptographic randomness under a t_ prefix.
    return "t_" + secrets.token_hex(4)


def add(text, col="todo", **note) -> dict:
    """Add a card to a column. Cards are id-bearing (Hermes task ids)."""
    text = str(text).strip()
    if not text:
        return {"ok": False, "error": "kanban: text is required"}
    cols = board()["cols"]
    if col not in cols:
        col = "todo"
    # Hermes kanban_list caps the readable working set; the JSON board caps
    # per-column growth instead (ids are one per card, never reused).
    if len(cols[col]) >= 200:
        return {"ok": False, "error": f"kanban: column {col!r} is full (200 cards)"}
    card = {"id": _new_task_id(), "text": text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if note:
        card.update(note)  # Hermes-style metadata rides on the card
    cols[col].append(card)
    res = _save(cols)
    if not res.get("ok"):
        return res
    return {"ok": True, "card": card, "col": col}


def move(card_id, to_col, pos=None) -> dict:
    """Move one card to another column, preserving column order.

    ``pos`` (0-based index within the target column) reorders on arrival.
    Unknown column -> todo, matching the shim's fallback; missing card ->
    ``ok: False``.
    """
    b = board()
    if not b.get("ok"):
        return b
    cols = b["cols"]
    if to_col not in cols:
        to_col = "todo"
    src = None
    card = None
    for col in _COLS:
        for i, c in enumerate(cols[col]):
            if c.get("id") == card_id:
                src, card, idx = col, c, i
                break
        if src is not None:
            break
    if src is None:
        return {"ok": False, "error": f"kanban: card {card_id} not found"}
    if pos is None:
        pos = len(cols[to_col])
    if not isinstance(pos, int) or pos < 0:
        return {"ok": False, "error": "kanban: pos must be a non-negative integer"}
    cols[src].pop(idx)
    card["moved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cols[to_col].insert(min(pos, len(cols[to_col])), card)
    res = _save(cols)
    if not res.get("ok"):
        return res
    return {"ok": True, "card": card, "col": to_col,
            "status": _status_of(to_col), "pos": pos}


def list() -> dict:
    """Board listing — columns in source order, cards in board order.

    Mirrors Hermes kanban_list: a stable order (there it is
    ``priority DESC, created_at ASC`` — with no priority, the JSON
    append order is the creation order) and the full board shape.
    """
    return board()