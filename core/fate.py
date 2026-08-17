#!/usr/bin/env python3
"""Fate layer — Atropos-original creativity (v18 E).

"Thread of Fate": every long-running operation gets a fate line telling
its story ("Clotho spins your backup…" → "✓ Thread woven"). Layers:

  - ``fate_today()`` — date-seeded sentence about what today holds for
    the system (different from the oracle line: oracle = random wisdom,
    fate = today's forecast). Same day → same fate (deterministic).
  - ``fate_line(story)`` — weave-voice status text for a running op.
  - ``WeaveCounter`` — total operations woven since first boot, persisted
    in ``~/.atropos/fate.json`` (weave counter; GitHub-contribution-style
    stat, not a graph yet).
  - ``cut_animation()`` — 3 ASCII frames of a thread being cut and
    re-woven (only used by ``doctor --fix``); 100ms per frame.
  - ``story(name, kind)`` — a short mythic short-story for ``lore --fate``,
    read from ``languages/*.json`` under ``lore_stories`` (fallback en).

Deadpan-warm voice: sharp, no cringe, no pet names, nothing a human
wouldn't actually say. No banned AI-isms.
"""
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import detect, i18n

_BAN = re.compile(r"(empower|seamless|leverage|unlock|delve|elevate|robust|"
                  r"cutting-edge|streamline|supercharge|unleash|effortless|"
                  r"game-?changer|revolutioniz)", re.I)


def _state_path() -> Path:
    return detect.atropos_home() / "fate.json"


def _load() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict):
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    _state_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── daily fate (date-seeded; stable within a day) ───────────────────────────
_FATES = [
    "The threads hold. No cutting needed today.",
    "Frayed strands ahead — backup before you weave.",
    "The loom hums. Today is for finishing, not starting.",
    "Three hands on one shuttle; mind the crossings.",
    "A thin thread wants attention. Doctor it early.",
    "The Fates are tidy today. Nothing dangling.",
    "News will arrive by wire. Read it twice before acting.",
    "One knot to undo, one to tie. Pick the order well.",
    "The shears rest today. Let them.",
    "Pattern repeats — yesterday's fix still holds.",
]


def fate_today(lang: str | None = None) -> str:
    """Today's fate line — stable across the day, seeded by date ordinal."""
    try:
        lines = i18n._load(lang or i18n.get_lang()).get("fate_lines")
        if not isinstance(lines, list) or not lines:
            lines = _FATES
        return lines[date.today().toordinal() % len(lines)]
    except Exception:
        return _FATES[0]


# ── fate lines for running operations (Thread of Fate) ─────────────────────
_STORIES = {
    "backup": ("Clotho spins your backup", "Thread woven into the vault"),
    "update": ("Lachesis measures the diff", "Thread measured"),
    "doctor": ("Atropos walks the loom", "Loom walked — thread sound"),
    "sync": ("Threads pulled to the far loom", "Far loom answered"),
    "agent": ("A new hand takes the shuttle", "Hand returned the shuttle"),
    "search": ("Fates scan the woven record", "Record read"),
}


def fate_line(story: str, done: bool = False, verify: bool = True) -> str:
    """One-line fate status for an operation.

    With verify=True, returns plain text (no ✓) so callers can animate.
    done=True returns the completion line (the cut).
    """
    spin, done_l = _STORIES.get(story, (f"{story.title()} runs", f"{story.title()} done"))
    out = done_l if done else spin + "…"
    if not verify and not done:
        return out
    return out


def story(name: str, lang: str | None = None) -> str:
    """A 5-10 line mythic short story (lore easter egg)."""
    try:
        pool = i18n._load(lang or i18n.get_lang()).get("lore_stories")
        if not isinstance(pool, dict) or not pool:
            pool = i18n._load("en").get("lore_stories") or {}
        text = pool.get(name) or (pool.get("default") or "")
    except Exception:
        text = ""
    if not text:
        text = ("The three sisters sat at the loom while the machine slept.\n"
                "Clotho wound the thread of intent; Lachesis measured what was asked;\n"
                "Atropos watched the edges for what frayed.\n"
                "When the machine woke it found nothing broken — only woven.")
    # never ship an AI-ism through the lore voice
    return text if not _BAN.search(text) else _FATES[0]


# ── weave counter — threads since first boot ────────────────────────────────
def weave(bump: int = 0) -> int:
    """Total operations woven since first boot. bump adds to the count."""
    state = _load()
    count = int(state.get("woven", 0))
    if bump:
        count += bump
        state["woven"] = count
        state["last"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save(state)
    return count


def weave_stats() -> dict:
    """Dashboard stat: {woven, first_boot, last_woven}."""
    state = _load()
    return {
        "woven": int(state.get("woven", 0)),
        "first_boot": state.get("first_boot", ""),
        "last_woven": state.get("last", ""),
    }


def mark_first_boot():
    """Record first-boot (wizard greeting / first status call)."""
    state = _load()
    if not state.get("first_boot"):
        state["first_boot"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save(state)


# ── cut animation (doctor --fix only) — 3 frames, 100ms each ────────────────
def cut_animation() -> list:
    """Three frames: thread whole → cut → re-woven. ASCII-only (TUI-safe)."""
    return [
        [" ║ ║ ", " ║ ║ ", "┃ ║ ║ ┃"],
        [" ║   ", "   ║ ", "  ✂   "],
        [" ╠╣  ", " ║║  ", " weave "],
    ]


def cut_line() -> str:
    """One-line cut+weave status for result boxes (non-animated use)."""
    return "─ thread cut, re-woven ─"