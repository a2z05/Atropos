"""Structured error codes + breadcrumbs (benchmark area 29 adoption).

Every error worth surfacing gets a stable machine-readable code
(``E_MODULE_NNN``), a human "what/why/fix" message, and a breadcrumb is
recorded before the failure — the trace-of-events pattern from Sentry,
implemented with stdlib collections only.

Usage::

    from . import errors
    errors.code("E_ROUTER_001")          # -> "E_ROUTER_001: router ping failed. nain did not respond. fix: check NINEROUTER_URL."
    errors.breadcrumb("router", "ping nain -> timeout")

The breadcrumb ring is a process-wide deque (maxlen 200) exposed to
doctor output and watch logs so self-healing decisions can cite the
trail that led there.
"""
import collections
import time

# ── error code table: code -> (what, why, fix) ─────────────────────────────
_CODES = {
    "E_ROUTER_001": ("router ping failed",
                     "the active router did not respond",
                     "check NINEROUTER_URL / api key, then `atropos route test`"),
    "E_ROUTER_002": ("router health degraded",
                     "more than one router failed a ping",
                     "check provider status, then `atropos doctor --fix`"),
    "E_CONFIG_001": ("config unreadable",
                     "config.yaml failed to parse",
                     "run `atropos settings export` and fix the YAML"),
    "E_SETTINGS_001": ("unknown setting",
                       "the key is not in the schema",
                       "run `atropos settings` to see valid keys"),
    "E_DB_001": ("database unavailable",
                 "chat.db could not be opened",
                 "check disk space and `atropos doctor`"),
    "E_BACKUP_001": ("backup failed",
                     "the backup step raised",
                     "check target storage, then `atropos backup`"),
    "E_WEBHOOK_001": ("webhook delivery failed",
                      "the endpoint rejected or timed out",
                      "check the URL, then `atropos webhooks test <name>`"),
    "E_API_001": ("internal api error",
                  "an api handler raised",
                  "see the dashboard log panel for the trace"),
}


def code(key: str) -> str:
    """Human "what. why. fix:" message for an error code (or the bare key)."""
    entry = _CODES.get(key)
    if not entry:
        return key
    what, why, fix = entry
    return f"{key}: {what}. {why}. fix: {fix}."


def register(key: str, what: str, why: str, fix: str):
    """Register a new error code at runtime (modules can extend the table)."""
    _CODES[key] = (what, why, fix)


def all_codes() -> dict:
    """The full code table (for docs/reference)."""
    return dict(_CODES)


# ── breadcrumb ring ─────────────────────────────────────────────────────────
_CRUMBS = collections.deque(maxlen=200)


def breadcrumb(category: str, message: str, level: str = "info"):
    """Record one breadcrumb: category + message + level + timestamp."""
    _CRUMBS.append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "category": category,
        "level": level,
        "msg": message,
    })


def trail(limit: int = 50) -> list:
    """Most recent breadcrumbs (newest first), capped at ``limit``."""
    items = list(_CRUMBS)[-limit:]
    return list(reversed(items))


def clear():
    """Empty the ring (used in tests)."""
    _CRUMBS.clear()