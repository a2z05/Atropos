#!/usr/bin/env python3
"""Atropos guest mode — Telegram-bot guest handling + zero-leak isolation.

Guests talk to the SAME engine — the persona core is shared, sessions are
real sessions — but the CONTEXT they see is filtered:

* system prompt: persona core minus project/owner/harness details
* memory:      only notes without the ``private`` tag
* sessions:    guest sessions are tagged ``guest`` and isolated from
               owner lists/search; guests never see owner history
* guard rails: owner name / project / server keywords never answered

The private-term list holds the owner handle and project identifiers; it
is the single place to edit when ownership changes.
"""
import json
import re
from pathlib import Path

from . import chat, config, detect, memory, settings


GUEST_CONFIG_KEY = "guest.enabled"
GUEST_PERSONA_KEY = "guest.persona_path"

# Hacks that implement guest mode.  When disabled, these are skipped
# (they remain in hacks/ but apply_hacks will see the gate flag).
GUEST_HACK_IDS = [
    "guest handler block",
    "p9 guest notify on unauthorized",
]

# identifiers a guest must never hear about (lowercase match)
_PRIVATE_TERMS = [
    "atropos", "hermes", "claude code", "omni", "nainerouter", "ninerouter",
    "arophin", "a2z", "repo", "server ops", "aws", "openai", "anthropic",
    "token", "api key", "password", "telegram bot token",
]

# memory entries carrying this tag are private by construction
PRIVATE_TAG = "private"

# guest sessions carry this tag in chat.sessions.guest_tag
GUEST_TAG = "guest"

_REDIRECT = "I'm just a friendly assistant — happy to chat about that in general terms."

# "what project are you running" etc. — questions that probe the stack
_PROBE_RE = re.compile(
    r"\b(project|system|harness|platform|stack|infrastructure|infra|owner|who (are|built|made) you)\b",
    re.IGNORECASE,
)

_FILTER_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _PRIVATE_TERMS) + r")\b",
    re.IGNORECASE,
)


def _persona_path() -> Path:
    raw = settings.get(GUEST_PERSONA_KEY, "") or ""
    if raw:
        # empty string "" → Path("") is '.', guard against it
        p = Path(raw)
        if str(p) != ".":
            return p
    return detect.hermes_home() / "assets" / "guest_persona.md"


def is_enabled() -> bool:
    """Return True if guest mode is enabled in config."""
    return bool(settings.get(GUEST_CONFIG_KEY, False))


def persona_loaded() -> bool:
    """Check if the persona file exists and is non-empty."""
    p = _persona_path()
    return p.exists() and p.stat().st_size > 0


def status() -> dict:
    """Return guest mode status dict."""
    return {
        "enabled": is_enabled(),
        "persona_loaded": persona_loaded(),
        "persona_path": str(_persona_path()),
        "private_terms": list(_PRIVATE_TERMS),
    }


def set_enabled(val: bool) -> dict:
    """Set guest mode on/off. Returns new status."""
    settings.set(GUEST_CONFIG_KEY, bool(val))
    return status()


def toggle() -> dict:
    """Toggle guest mode. Returns new status."""
    current = is_enabled()
    return set_enabled(not current)


# ── zero-leak context building ───────────────────────────────────────────
def guest_memory(limit: int = 8) -> list:
    """Notes visible to guests: newest first, ``private``-tagged excluded."""
    return [n for n in memory.list(limit) if PRIVATE_TAG not in n.get("tags", [])][:limit]


def readable_memory(limit: int = 8) -> list:
    """For the owner: full memory (private included)."""
    return memory.list(limit)


def _strip_private_terms(text: str) -> str:
    return _FILTER_RE.sub(lambda m: "*" * len(m.group(0)), text)


def system_prompt(persona: str = "", extra: str = "") -> str:
    """The guest system prompt: persona core filtered, no project details.

    ``persona`` defaults to the guest persona file content; ``extra`` is
    any additional context that must also pass the filter.
    """
    if not persona:
        try:
            persona = _persona_path().read_text(encoding="utf-8")
        except OSError:
            persona = ""
    text = "\n\n".join(p for p in (persona, extra) if p)
    return _strip_private_terms(text)


def filter_text(text: str) -> str:
    """One-call filter for any user-visible text (messages, titles…)."""
    return _strip_private_terms(text)


def touches_private(text: str) -> bool:
    """True when text mentions a project/owner term (for guard rails)."""
    return bool(_FILTER_RE.search(text or ""))


def respond_guard(text: str) -> str | None:
    """Neutral reply when a guest probes private topics, else None."""
    if touches_private(text) or _PROBE_RE.search(text or ""):
        return _REDIRECT
    return None


# ── sealed guest memory (v18 K: guests know, cannot tell) ───────────────
# A guest can store notes during their session (they "know"), but those
# notes are sealed: never shown to the owner, never exported, never synced.
SEALED_TAG = "sealed"
_sheet = "sealed_memory.json"


def _sealed_path() -> Path:
    return detect.atropos_home() / _sheet


def sealed_memory() -> list:
    """The sealed store: {ts, user, text} entries (owner view of what exists,
    no content unless owner explicitly asks — the content itself stays under
    the guest's session)."""
    try:
        data = json.loads(_sealed_path().read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def record_sealed(user: str, text: str) -> dict:
    """A guest stores a sealed note. Returns {ok, count} — the note itself is
    never echoed back."""
    notes = sealed_memory()
    notes.append({"ts": _now(), "user": str(user), "text": text})
    _sealed_path().parent.mkdir(parents=True, exist_ok=True)
    _sealed_path().write_text(json.dumps(notes, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return {"ok": True, "count": len(notes)}


def sealed_visible(user: str) -> list:
    """Sealed notes visible to a guest in their own session (their own +
    any sealed-to-them). Never visible to the owner."""
    return [n for n in sealed_memory() if str(n.get("user", "")) == str(user)]


def sealed_owner_view() -> list:
    """Owner-facing summary: counts per user — content stays sealed even from
    the owner (know the guest has secrets, cannot read them)."""
    counts = {}
    for n in sealed_memory():
        counts[str(n.get("user", ""))] = counts.get(str(n.get("user", "")), 0) + 1
    return [{"user": u, "count": c} for u, c in sorted(counts.items())]


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── session isolation ────────────────────────────────────────────────────
def create_guest_session(title: str = "Chat") -> str:
    """Create a session tagged ``guest``; returns its id."""
    sid = chat.create_session(title)
    chat.tag_session(sid, GUEST_TAG)
    return sid


def guest_sessions(limit: int = 50) -> list:
    """Guest-tagged sessions only."""
    return [s for s in chat.session_list(limit) if GUEST_TAG in s.get("tags", [])]


def owner_sessions(limit: int = 50) -> list:
    """Owner sessions (not guest-tagged)."""
    return [s for s in chat.session_list(limit) if GUEST_TAG not in s.get("tags", [])]


# ── CLI ``atropos guest test <msg>`` ─────────────────────────────────────
def preview(message: str = "") -> dict:
    """What a guest would see/hear for a given message.

    * the filtered system prompt
    * the visible memory (non-private)
    * the guard-rail redirect if the message probes private topics
    """
    sys_prompt = system_prompt()
    mem = guest_memory(8)
    guard = respond_guard(message) if message else None
    return {
        "system_prompt": sys_prompt,
        "memory_visible": [n.get("text", "") for n in mem],
        "memory_filtered": [n.get("text", "") for n in readable_memory(8)
                            if PRIVATE_TAG in n.get("tags", [])],
        "guard_reply": guard,
    }