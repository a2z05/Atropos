#!/usr/bin/env python3
"""Atropos announcement feed — tips, changelog notices, version checks.

The feed persists in ``~/.atropos/announce.json``:

    {changelog_head, tip_of_day, version_check: {...}, dismissed: []}

* ``tip``      — rotating usage tips from the bundled list of 12 below,
                 one per calendar day (``tip_of_day`` key), each
                 dismissible,
* ``changelog`` — one notice per release, raised after ``version``
                  changes and cleared by ``mark_changelog_seen``,
* ``version``  — result of the latest ``update_check``, kept by the
                 dashboard as an inline reminder.

``dismissed`` records the ids of feed items the owner has dismissed so
they are never offered again. All reads are defensive: a corrupt or
missing file yields an empty feed instead of an exception.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, detect, update

CHANGELOG_URL = "https://github.com/a2z/atropos/blob/main/docs/CHANGELOG.md"

_TIPS = [
    "Type /status in the chat to see the router, model and uptime at a glance.",
    "Slash commands in chat run through the console whitelist — only registered commands are allowed.",
    "The dashboard remembers your token per-browser; the password gate is only for the token entry screen.",
    "Backups: set backup.period to daily and the watch daemon creates one for you.",
    "Router failover is automatic: nain → omni → local after consecutive failures.",
    "The mobile chat page is a PWA — add it to your home screen for an app-like experience.",
    "Effort tiers (minimal → tryhard) tune how deep each harness works; chat sends use chat.effort.",
    "Doctor is the health check: `atropos doctor` runs the same checks the dashboard shows.",
    "The ⌘K palette jumps to any panel — try it on the dashboard.",
    "New devices hitting the LAN share page wait in the Pairing panel until you approve them.",
    "The ASCII QR frame in the terminal is decorative — the URL next to it is the real way to share.",
    "Updates apply with a snapshot first, so you can roll back if a patch misbehaves.",
]


# ── persistence ───────────────────────────────────────────────────────────
def announce_path() -> Path:
    """Announcement store location: ``~/.atropos/announce.json``."""
    return detect.atropos_home() / "announce.json"


def _load() -> dict:
    p = announce_path()
    if not p.exists():
        return {"changelog_head": None, "tip_of_day": None,
                "version_check": {}, "dismissed": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"changelog_head": None, "tip_of_day": None,
                "version_check": {}, "dismissed": []}
    if not isinstance(data, dict):
        return {"changelog_head": None, "tip_of_day": None,
                "version_check": {}, "dismissed": []}
    data.setdefault("changelog_head", None)
    data.setdefault("tip_of_day", None)
    data.setdefault("version_check", {})
    data.setdefault("dismissed", [])
    return data


def _save(data: dict):
    p = announce_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _version() -> str:
    """Version from the repo VERSION file via the dashboard's reader."""
    from . import dashboard as _dashboard
    try:
        return _dashboard.api_version().get("version", "1.0.0")
    except Exception:
        return "1.0.0"


# ── feed ──────────────────────────────────────────────────────────────────
def feed() -> list:
    """The announcement feed: tip + changelog + version items.

    Returns ``[{id, type, text, dismissible, ts}]``. Tips rotate daily
    (one per calendar day from the bundled list); the changelog notice
    appears once per release until ``mark_changelog_seen``; a version
    item appears when the last ``update_check`` reported commits behind.
    Dismissed ids are never returned again.
    """
    data = _load()
    dismissed = set(data.get("dismissed") or [])
    items = []
    now = _now()

    if data.get("tip_of_day") != _today():
        data["tip_of_day"] = _today()
        _save(data)
    tip_text = _TIPS[date.today().toordinal() % len(_TIPS)]
    if "tip:today" not in dismissed:
        items.append({
            "id": "tip:today", "type": "tip", "text": tip_text,
            "dismissible": True, "ts": now,
        })

    head = _version()
    if data.get("changelog_head") != head and "changelog:latest" not in dismissed:
        items.append({
            "id": "changelog:latest", "type": "changelog",
            "text": f"Atropos {head} is live — what's new: {CHANGELOG_URL}",
            "dismissible": True, "ts": now,
        })

    vc = data.get("version_check") or {}
    if vc.get("ok") and not vc.get("up_to_date") and "version:behind" not in dismissed:
        items.append({
            "id": "version:behind", "type": "version",
            "text": f"Update available: {vc.get('behind', 0)} commit(s) behind"
                    f" ({vc.get('head', '?')} → {vc.get('remote', '?')})."
                    f" Run /update apply or check the Update panel.",
            "dismissible": True, "ts": now,
        })
    return items


def dismiss(item_id: str) -> bool:
    """Record a dismissal so the item is never shown again."""
    data = _load()
    dismissed = list(data.get("dismissed") or [])
    if item_id not in dismissed:
        dismissed.append(item_id)
        data["dismissed"] = dismissed[-500:]
        _save(data)
    return True


def mark_changelog_seen(version: str) -> None:
    """Acknowledge the current changelog so the notice clears."""
    data = _load()
    data["changelog_head"] = version or _version()
    _save(data)


def set_version_check(result: dict) -> None:
    """Persist the latest ``update_check`` result for the feed."""
    data = _load()
    if isinstance(result, dict):
        data["version_check"] = {
            k: result.get(k) for k in ("ok", "up_to_date", "behind", "head", "remote")
        }
    _save(data)


def run_version_check() -> dict:
    """Run ``update.update_check`` and persist the result (never raises)."""
    repo = detect.hermes_agent()
    result = update.update_check(repo) if repo else {"ok": False, "error": "no repo"}
    set_version_check(result)
    return result


def _tip_of_day() -> str:
    """The tip for today (exposed for tests)."""
    return _TIPS[date.today().toordinal() % len(_TIPS)]


if __name__ == "__main__":
    for item in feed():
        print(f"[{item['type']}] {item['text']}")
