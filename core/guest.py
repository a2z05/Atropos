#!/usr/bin/env python3
"""Atropos guest mode — enable/disable guest handling + persona management.

Guest mode lets unauthorized Telegram users talk to the bot via inline
guest queries. The feature is toggled by writing a flag into the Atropos
config and re-applying (or skipping) the relevant hacks (04 + 12).
"""
import json
from pathlib import Path

from . import config, detect, patches, settings


GUEST_CONFIG_KEY = "guest.enabled"
GUEST_PERSONA_KEY = "guest.persona_path"

# Hacks that implement guest mode.  When disabled, these are skipped
# (they remain in hacks/ but apply_hacks will see the gate flag).
GUEST_HACK_IDS = [
    "guest handler block",
    "p9 guest notify on unauthorized",
]


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
    }


def set_enabled(val: bool) -> dict:
    """Set guest mode on/off. Returns new status."""
    settings.set(GUEST_CONFIG_KEY, bool(val))
    return status()


def toggle() -> dict:
    """Toggle guest mode. Returns new status."""
    current = is_enabled()
    return set_enabled(not current)
