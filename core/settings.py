#!/usr/bin/env python3
"""Atropos settings — single source of truth for every config key.

Layered on top of core/config.py (whose DEFAULTS stay untouched for
back-compat and for tests/test_core.py). This module owns:

  * the SETTINGS_SCHEMA — every key the system reads, with type/default/
    group/choices + secret flags,
  * typed, validated access — ``get`` (coercion, unknown → None) and
    ``set`` (unknown key → ValueError, type mismatch → ValueError),
  * migration of legacy nested keys (alerts.thresholds.disk → flat),
  * secret masking for exports and API responses,
  * export/import of the full configuration as YAML.

The config *file* is still ~/.atropos/config.yaml written via
config.save(); schema keys map onto the nested legacy shape so
config.DEFAULTS-based code keeps working.

Usage:
    from core import settings
    settings.get("dashboard.port")        # 8787
    settings.set("dashboard.port", "8787")   # coerced to int
    settings.set("dashboard.port", "abc")    # ValueError
"""
import json
import re
import sys
from copy import deepcopy

from . import config, detect

# ── constants shared with the CLI / dashboard ─────────────────────────────
EFFORT_TIERS = ["minimal", "low", "medium", "high", "xhigh", "ultracode", "tryhard"]
ROUTER_NAMES = ["nain", "omni", "local"]
THEMES = ["black", "dark", "light", "sepia", "midnight", "matrix", "ink", "embers", "glass", "auto"]
ACCENTS = ["indigo", "cyan", "green", "amber", "violet"]
LANGS = ["en", "fa", "de", "fr", "es", "ru", "ar", "tr", "zh", "hi", "it"]
RTL_LANGS = ["fa", "ar", "he", "ur"]
SECRET_MASK = "***"

# Keep in sync with core/skills.py DEFAULT_ROUTING.
_DEFAULT_ROUTING = {
    "coding": "claude",
    "devops": "claude",
    "debugging": "claude",
    "research": "hermes",
    "creative": "hermes",
    "productivity": "hermes",
    "social-media": "hermes",
    "mlops": "claude",
    "telegram": "hermes",
    "trading": "hermes",
    "general": "hermes",
}


# ── the schema ────────────────────────────────────────────────────────────
# key → {type, default, group, description, choices?, min?, max?, secret?}
# Types: string | int | bool | choice | list | map. "readonly" keys are
# computed/legacy and never validated on write.
SETTINGS_SCHEMA = {
    # ---- core ----
    "router.active": {"type": "choice", "default": "nain", "choices": ROUTER_NAMES,
                      "group": "core", "description": "Active router (nain / omni / local only)"},
    "router.model": {"type": "string", "default": "deepmo", "group": "core",
                     "description": "Model served by the active router"},
    "router.base_url": {"type": "string", "default": "", "group": "core",
                        "description": "Base URL override (empty = env default)"},
    "router.api_key_env": {"type": "string", "default": "OPENAI_API_KEY", "group": "core",
                           "description": "Env var holding the router API key"},
    "effort.hermes": {"type": "choice", "default": "medium", "choices": EFFORT_TIERS,
                      "group": "core", "description": "Hermes effort tier"},
    "effort.claude": {"type": "choice", "default": "medium", "choices": EFFORT_TIERS,
                      "group": "core", "description": "Claude Code effort tier"},
    "effort.atropos": {"type": "choice", "default": "medium", "choices": EFFORT_TIERS,
                       "group": "core", "description": "Atropos effort tier"},
    "effort_default": {"type": "choice", "default": "medium", "choices": EFFORT_TIERS,
                       "group": "core",
                       "description": "Fallback tier when a per-harness key is absent"},
    "update.channel": {"type": "choice", "default": "stable", "choices": ["stable", "beta"],
                       "group": "core", "description": "Update channel"},
    "update.changelog_bump": {"type": "bool", "default": True, "group": "core",
                              "description": "Auto-prepend a changelog entry after apply"},
    "update.auto": {"type": "choice", "default": "off", "choices": ["off", "check", "apply"],
                    "group": "core",
                    "description": "Auto-update mode: off = manual, check = alert only, apply = auto-apply clean updates"},
    "update.auto_ai": {"type": "bool", "default": False, "group": "core",
                       "description": "Let the AI update engine fix conflict updates when update.auto=apply"},
    # ---- cli ----
    "cli.default_action": {"type": "choice", "default": "cli",
                           "choices": ["cli", "dashboard", "both", "menu", "repl"], "group": "cli",
                           "description": "What a bare `atropos` (no subcommand) invokes: cli|dashboard|both|menu|repl"},
    "cli.theme": {"type": "choice", "default": "dark",
                  "choices": ["dark", "light", "black"], "group": "cli",
                  "description": "Terminal theme for the CLI banner/status (light|dark|black)"},
    "cli.lang": {"type": "choice", "default": "en",
                 "choices": ["en", "fa", "de", "fr", "es", "ru", "ar", "tr", "zh", "hi", "it"],
                 "group": "cli",
                 "description": "Language for CLI output (missing keys fall back to English)"},
    # computed/legacy — present in DEFAULTS, never user-writable
    "version": {"type": "string", "default": "1.0.0", "group": "core", "readonly": True,
                "description": "Legacy config version key (actual version = VERSION file)"},
    "hermes.home": {"type": "string", "default": "", "group": "core", "readonly": True,
                    "description": "Computed: detect.hermes_home()"},
    "hermes.log_channel": {"type": "string", "default": "", "group": "core", "readonly": True,
                           "description": "Computed: TELEGRAM_LOG_CHANNEL / ATRA_LOG_CHANNEL"},
    "claude.model": {"type": "string", "default": "sonnet", "group": "core",
                     "description": "Claude model alias (defaults only)"},
    "claude.alias": {"type": "string", "default": "nain", "group": "core",
                     "description": "Claude router alias (defaults only)"},
    # ---- watch ----
    "watch.interval": {"type": "int", "default": 1800, "min": 60, "group": "watch",
                       "description": "Watch daemon interval in seconds"},
    "watch.threshold_disk": {"type": "int", "default": 80, "min": 10, "max": 99, "group": "watch",
                             "description": "Disk alert threshold percent"},
    "watch.auto_backup": {"type": "bool", "default": True, "group": "watch",
                          "description": "Create a backup automatically when period=daily is due"},
    "watch.state_db_mb": {"type": "int", "default": 100, "min": 1, "max": 4096, "group": "watch",
                          "description": "state.db size alert threshold MB"},
    "watch.log_max_mb": {"type": "int", "default": 50, "min": 1, "group": "watch",
                         "description": "Log rotation threshold MB"},
    # ---- alerts ----
    "alerts.enabled": {"type": "bool", "default": True, "group": "alerts",
                       "description": "Master switch for Telegram alerts"},
    "alerts.token": {"type": "string", "default": "", "secret": True, "group": "alerts",
                     "description": "Telegram bot token (empty = hermes config fallback)"},
    "alerts.chat_id": {"type": "string", "default": "", "group": "alerts",
                       "description": "Telegram chat/channel id for alerts"},
    "alerts.threshold_disk": {"type": "int", "default": 80, "min": 10, "max": 99, "group": "alerts",
                              "description": "Disk percent that triggers a disk alert"},
    "alerts.latency_ms": {"type": "int", "default": 5000, "min": 1, "group": "alerts",
                          "description": "Router latency alert threshold ms"},
    "alerts.min_interval": {"type": "int", "default": 600, "min": 10, "group": "alerts",
                            "description": "Per-event alert rate limit in seconds"},
    "alerts.events.disk": {"type": "bool", "default": True, "group": "alerts",
                           "description": "Alert on disk usage"},
    "alerts.events.doctor": {"type": "bool", "default": True, "group": "alerts",
                             "description": "Alert on doctor failures"},
    "alerts.events.router": {"type": "bool", "default": True, "group": "alerts",
                             "description": "Alert on router down / failover"},
    "alerts.events.patches": {"type": "bool", "default": True, "group": "alerts",
                              "description": "Alert on patch drift"},
    # ---- dashboard ----
    "dashboard.port": {"type": "int", "default": 8787, "min": 1, "max": 65535, "group": "dashboard",
                       "description": "Dashboard listen port"},
    "dashboard.host": {"type": "string", "default": "127.0.0.1", "group": "dashboard",
                       "description": "Dashboard bind host"},
    "dashboard.password": {"type": "string", "default": "", "secret": True, "group": "dashboard",
                           "description": "Optional password gate before token entry"},
    "dashboard.refresh_ms": {"type": "int", "default": 10000, "min": 1000, "group": "dashboard",
                             "description": "Frontend panel refresh interval ms"},
    "dashboard.theme": {"type": "choice", "default": "auto", "choices": THEMES, "group": "dashboard",
                        "description": "dark | light | auto (follows system)"},
    "dashboard.lang": {"type": "choice", "default": "en", "choices": LANGS, "group": "dashboard",
                       "description": "en | fa (Farsi is RTL)"},
    "dashboard.accent": {"type": "choice", "default": "indigo", "choices": ACCENTS, "group": "dashboard",
                         "description": "Accent color"},
    "dashboard.particles": {"type": "bool", "default": True, "group": "dashboard",
                            "description": "Animated particle canvas"},
    "dashboard.live": {"type": "bool", "default": True, "group": "dashboard",
                       "description": "SSE live updates for panels"},
    # ---- backup ----
    "backup.period": {"type": "choice", "default": "off", "choices": ["daily", "off"], "group": "backup",
                      "description": "daily | off — watch daemon auto-creates when daily"},
    "backup.retention": {"type": "int", "default": 5, "min": 1, "max": 30, "group": "backup",
                         "description": "Number of recent backups to keep"},
    "backup.retention_weekly": {"type": "int", "default": 4, "min": 0, "max": 26, "group": "backup",
                                "description": "Extra weekly backups to keep (keep-N + weekly-M)"},
    "backup.backend": {"type": "choice", "default": "file", "group": "backup",
                       "choices": ["file", "s3", "server", "github", "pair"],
                       "description": "Default backup backend"},
    "backup.server.url": {"type": "string", "default": "", "group": "backup",
                          "description": "Self-hosted backup server base URL"},
    "backup.server.token": {"type": "string", "default": "", "secret": True, "group": "backup",
                            "description": "Self-hosted backup server token"},
    "backup.s3.endpoint": {"type": "string", "default": "", "group": "backup",
                           "description": "S3-compatible endpoint (AWS/MinIO/B2/Wasabi)"},
    "backup.s3.bucket": {"type": "string", "default": "", "group": "backup",
                         "description": "S3 bucket name"},
    "backup.s3.region": {"type": "string", "default": "us-east-1", "group": "backup",
                         "description": "S3 region"},
    "backup.s3.access_key": {"type": "string", "default": "", "secret": True, "group": "backup",
                             "description": "S3 access key id"},
    "backup.s3.secret_key": {"type": "string", "default": "", "secret": True, "group": "backup",
                             "description": "S3 secret access key"},
    # ---- guest ----
    "guest.enabled": {"type": "bool", "default": False, "group": "guest",
                      "description": "Guest mode on/off"},
    "guest.persona_path": {"type": "string", "default": "", "group": "guest",
                           "description": "Guest persona file (empty = hermes assets default)"},
    # ---- skills ----
    "skills.routing": {"type": "map", "default": _DEFAULT_ROUTING.copy(), "group": "skills",
                       "description": "Category → harness (hermes|claude) routing"},
    "skills.auto_sync": {"type": "bool", "default": True, "group": "skills",
                         "description": "Auto-sync universal skills into Hermes"},
    "skills.auto_skill": {"type": "bool", "default": False, "group": "skills",
                          "description": "Offer to save a skill after a complex task (v18 F)"},
    "skills.auto_memory": {"type": "bool", "default": False, "group": "skills",
                           "description": "Offer [Save to memory] after sessions (v18 F)"},
    # ---- jailbreak ----
    "jailbreak.auto_apply": {"type": "bool", "default": False, "group": "jailbreak",
                             "description": "Re-apply all jailbreak bypasses on doctor --fix"},
    # ---- failover ----
    "failover.enabled": {"type": "bool", "default": True, "group": "failover",
                         "description": "Router auto-failover on consecutive pings"},
    "failover.order": {"type": "list", "default": ["nain", "omni", "local"], "group": "failover",
                       "item_choices": ROUTER_NAMES,
                       "description": "Failover order (nain → omni → local)"},
    "failover.retries": {"type": "int", "default": 2, "min": 1, "max": 10, "group": "failover",
                         "description": "Consecutive failures before switching"},
    "failover.hold_minutes": {"type": "int", "default": 60, "min": 1, "group": "failover",
                              "description": "Minutes to hold a manual router choice before failover resumes"},
    # ---- extensions ----
    "extensions.enabled": {"type": "bool", "default": True, "group": "extensions",
                           "description": "Universal extension layer master switch"},
    # ---- routing hub (which harness handles which task) ----
    "routing.enabled": {"type": "bool", "default": True, "group": "routing",
                        "description": "Task routing hub master switch"},
    "routing.default": {"type": "choice", "default": "auto",
                        "choices": ["clotho", "lachesis", "atropos", "auto"], "group": "routing",
                        "description": "Fallback harness for unmatched tasks"},
    "routing.map": {"type": "map", "default": {}, "group": "routing",
                    "description": "Category → harness override (clotho|lachesis|atropos|auto)"},
    # ---- universal MCP ----
    "mcp.enabled": {"type": "bool", "default": True, "group": "mcp",
                    "description": "Universal MCP registry master switch"},
    "mcp.adopt_ask": {"type": "bool", "default": True, "group": "mcp",
                      "description": "Ask before importing newly discovered harness servers"},
    # ---- universal identity ----
    "identity.map": {"type": "map", "default": {}, "group": "identity",
                     "description": "Identity file → {targets, mode} deployment mapping"},
    # ---- universal configs ----
    "configs.mode": {"type": "choice", "default": "separate",
                     "choices": ["shared", "separate", "atropos-only"], "group": "configs",
                     "description": "Default deployment mode for config files"},
    # ---- LAN sharing ----
    "lan.enabled": {"type": "bool", "default": True, "group": "lan",
                    "description": "LAN sharing + device approval flow"},
    "lan.qr_ascii": {"type": "bool", "default": True, "group": "lan",
                     "description": "Print an ASCII QR code in the terminal on --share"},
    # ---- mobile chat ----
    "chat.enabled": {"type": "bool", "default": True, "group": "chat",
                     "description": "Mobile chat page + streaming endpoints"},
    "chat.effort": {"type": "choice", "default": "medium", "choices": EFFORT_TIERS, "group": "chat",
                    "description": "Default effort tier for chat sends"},
    # ---- fleet ----
    "fleet.enabled": {"type": "bool", "default": True, "group": "fleet",
                      "description": "Multi-box fleet health grid"},
    "fleet.refresh_ms": {"type": "int", "default": 30000, "min": 5000, "group": "fleet",
                         "description": "Fleet ping interval ms"},
    # ---- memory (RAG) ----
    "memory.enabled": {"type": "bool", "default": True, "group": "memory",
                       "description": "RAG memory keyword search"},
    "memory.k": {"type": "int", "default": 8, "min": 1, "max": 100, "group": "memory",
                 "description": "Default result count for memory search"},
    # ---- usage / quota gate ----
    "budget.enabled": {"type": "bool", "default": False, "group": "budget",
                       "description": "Per-router monthly token budget gate"},
    "budget.monthly_tokens": {"type": "int", "default": 0, "min": 0, "group": "budget",
                              "description": "Monthly token budget (0 = unlimited)"},
    "budget.auto_failover": {"type": "bool", "default": False, "group": "budget",
                             "description": "Auto-failover to a cheaper router when over budget"},
    # ---- one-shot share links ----
    "links.ttl_hours": {"type": "int", "default": 1, "min": 1, "max": 168, "group": "links",
                        "description": "One-shot share link lifetime hours"},
    # ---- activity timeline ----
    "activity.max_mb": {"type": "int", "default": 5, "min": 1, "max": 100, "group": "activity",
                        "description": "activity.jsonl rotation size MB"},
    # ---- snapshot gallery ----
    "snapshots.enabled": {"type": "bool", "default": True, "group": "snapshots",
                          "description": "Auto-snapshot before every update/apply"},
    # ---- multi-backend sync ----
    "sync.enabled": {"type": "bool", "default": True, "group": "sync",
                     "description": "Multi-backend sync master switch"},
    "sync.interval": {"type": "int", "default": 900, "min": 300, "max": 1800, "group": "sync",
                      "description": "Sync interval seconds (300-1800; manual-only when 0)"},
    "sync.manual_only": {"type": "bool", "default": False, "group": "sync",
                         "description": "Never auto-sync; only on explicit push/pull"},
    "sync.server.url": {"type": "string", "default": "", "group": "sync",
                        "description": "Self-hosted sync server base URL"},
    "sync.server.token": {"type": "string", "default": "", "secret": True, "group": "sync",
                          "description": "Self-hosted sync server token"},
    "sync.pair_ttl_hours": {"type": "int", "default": 1, "min": 1, "max": 24, "group": "sync",
                            "description": "Direct-pair code lifetime hours"},
    # ---- AI update engine ----
    "update-ai.model": {"type": "string", "default": "deepmo", "group": "update-ai",
                        "description": "Model used by the AI update engine"},
    "update-ai.effort": {"type": "choice", "default": "medium", "choices": EFFORT_TIERS,
                         "group": "update-ai", "description": "Effort tier for the AI update engine"},
    "update-ai.mode": {"type": "choice", "default": "manual",
                       "choices": ["auto", "manual", "off"], "group": "update-ai",
                       "description": "auto = apply on confirm, manual = preview only, off = never invoked"},
    # ---- logs / webhooks / permissions (monitored resources) ----
    "webhooks.enabled": {"type": "bool", "default": True, "group": "webhooks",
                         "description": "Universal webhook registry"},
    "permissions.preset": {"type": "choice", "default": "default",
                           "choices": ["default", "acceptEdits", "plan", "bypassPermissions"], "group": "permissions",
                           "description": "Claude permission preset projected via settings"},
    # ---- middleware (Filters & Plugins) ----
    "middleware.enabled": {"type": "list", "default": [],
                           "group": "middleware",
                           "description": "Ordered list of enabled filters (human names, e.g. pii, retry)"},
    # ---- telegram gateway ----
    "telegram.token": {"type": "string", "default": "", "secret": True, "group": "telegram",
                       "description": "Bot token for the built-in Telegram gateway"},
    "telegram.owner_ids": {"type": "list", "default": [], "group": "telegram",
                           "description": "Telegram user ids allowed full access"},
    "telegram.guests": {"type": "choice", "default": "allow",
                        "choices": ["allow", "readonly", "deny"], "group": "telegram",
                        "description": "What strangers can do: allow | readonly | deny"},
    # ---- appearance (dashboard + CLI/TUI themes) ----
    "theme": {"type": "choice", "default": "dark",
              "choices": ["black", "dark", "light", "sepia", "midnight", "matrix",
                          "ink", "embers", "glass"],
              "group": "dashboard",
              "description": "Global theme: black|dark|light|sepia|midnight|matrix|ink|embers|glass"},
    "beta_badge": {"type": "bool", "default": True, "group": "dashboard",
                   "description": "Show the BETA badge in the dashboard/TUI/CLI"},
}

GROUPS = [
    "core", "cli", "dashboard", "watch", "alerts", "backup",
    "guest", "skills", "jailbreak", "failover", "extensions",
    "routing", "mcp", "identity", "configs", "lan", "chat",
    "fleet", "memory", "budget", "links", "activity", "snapshots",
    "sync", "update-ai", "webhooks", "permissions", "middleware", "telegram",
]


def schema() -> dict:
    """Return a deep copy of the settings schema (safe to mutate)."""
    return deepcopy(SETTINGS_SCHEMA)


def groups() -> list:
    """Ordered group names for the settings table/panel."""
    return list(GROUPS)


def is_secret(key: str) -> bool:
    """True when the schema flags this key as secret (token/password)."""
    return bool(SETTINGS_SCHEMA.get(key, {}).get("secret"))


def mask_secrets(data: dict) -> dict:
    """Return a copy of a config dict with secret values masked."""
    out = deepcopy(data)
    for key, spec in SETTINGS_SCHEMA.items():
        if not spec.get("secret"):
            continue
        node = out
        parts = key.split(".")
        try:
            for part in parts[:-1]:
                node = node[part]
            if isinstance(node, dict) and node.get(parts[-1]) not in (None, ""):
                node[parts[-1]] = SECRET_MASK
        except (KeyError, TypeError):
            continue
    return out


# ── typed access ──────────────────────────────────────────────────────────
def _coerce(spec: dict, value):
    """Coerce a raw value to the schema type. Raises ValueError on mismatch."""
    t = spec["type"]
    if t == "string":
        if not isinstance(value, str):
            while isinstance(value, bool):  # bools are ints; reject explicitly
                break
            if isinstance(value, (int, float)):
                return str(value)
            raise ValueError(f"expected a string, got {type(value).__name__}")
        return value
    if t == "int":
        if isinstance(value, bool):
            raise ValueError("expected an integer, got a boolean")
        if isinstance(value, int):
            num = value
        elif isinstance(value, str):
            if not re.fullmatch(r"-?\d+", value.strip()):
                raise ValueError(f"expected an integer, got {value!r}")
            num = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            num = int(value)
        else:
            raise ValueError(f"expected an integer, got {type(value).__name__}")
        if "min" in spec and num < spec["min"]:
            raise ValueError(f"must be >= {spec['min']}")
        if "max" in spec and num > spec["max"]:
            raise ValueError(f"must be <= {spec['max']}")
        return num
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "yes", "on", "1"):
                return True
            if lowered in ("false", "no", "off", "0"):
                return False
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError(f"expected a boolean, got {value!r}")
    if t == "choice":
        if value not in spec.get("choices", []):
            choices = ", ".join(str(c) for c in spec.get("choices", []))
            raise ValueError(f"{value!r} is not one of: {choices}")
        return value
    if t == "list":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                raise ValueError(f"expected a JSON list, got {value!r}")
        if not isinstance(value, list):
            raise ValueError("expected a list")
        item_choices = spec.get("item_choices")
        if item_choices:
            for item in value:
                if item not in item_choices:
                    raise ValueError(f"{item!r} is not one of: {', '.join(item_choices)}")
        return list(value)
    if t == "map":
        if isinstance(value, str):
            # the legacy YAML dumper emits empty dicts as "key: " (empty
            # string) — treat that as the empty mapping
            if value.strip() == "":
                value = {}
            else:
                try:
                    value = json.loads(value)
                except Exception:
                    raise ValueError(f"expected a JSON object, got {value!r}")
        if not isinstance(value, dict):
            raise ValueError("expected a mapping")
        values_spec = spec.get("values")
        if values_spec and values_spec.get("type") == "choice":
            choices = values_spec.get("choices", [])
            for k, v in value.items():
                if v not in choices:
                    raise ValueError(f"{k}: {v!r} is not one of: {', '.join(choices)}")
        return dict(value)
    raise ValueError(f"unknown schema type: {t}")


def get(key: str, default=None):
    """Read a config value through the schema (typed, coerced).

    Unknown keys return ``default`` (mirroring config.get semantics).
    Missing values fall back to the schema default when present.
    """
    p = config.config_path()
    raw = {}
    if p.exists():
        try:
            raw = config.parse_yaml(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[settings] warning: failed to parse {p}: {e}", file=sys.stderr)
    node = raw
    parts = key.split(".")
    found = True
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            found = False
            break
        node = node[part]
    if not found:
        spec = SETTINGS_SCHEMA.get(key)
        if spec is None:
            return default
        return deepcopy(spec["default"])
    spec = SETTINGS_SCHEMA.get(key)
    if spec is None:
        return node if node is not None else default
    try:
        return _coerce(spec, node)
    except ValueError:
        return spec["default"] if default is None else default


def load() -> dict:
    """Full config loaded and normalized against the schema.

    Returns the nested legacy shape (same as config.load()) with every
    schema default merged in — this is what the dashboard/TUI display.
    """
    cfg = config.load()
    for key, spec in SETTINGS_SCHEMA.items():
        if spec.get("readonly"):
            continue
        node = cfg
        parts = key.split(".")
        try:
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            if parts[-1] not in node:
                node[parts[-1]] = deepcopy(spec["default"])
        except (KeyError, TypeError):
            continue
    # legacy nested alerts.thresholds → flat (no write here; migrate owns writes)
    _legacy_fold(cfg)
    return cfg


def _legacy_fold(cfg: dict):
    """Fold alerts.thresholds.disk/latency_ms into flat keys in-memory."""
    alerts = cfg.get("alerts")
    if not isinstance(alerts, dict):
        return
    thr = alerts.get("thresholds")
    if isinstance(thr, dict):
        if "threshold_disk" not in alerts and isinstance(thr.get("disk"), (int, float)):
            alerts["threshold_disk"] = int(thr["disk"])
        if "latency_ms" not in alerts and isinstance(thr.get("latency_ms"), (int, float)):
            alerts["latency_ms"] = int(thr["latency_ms"])
        alerts.pop("thresholds", None)


def set(key: str, value) -> dict:
    """Validate + write one key through the schema.

    Raises ValueError for unknown keys, wrong types and out-of-range
    values. Returns the full normalized config.
    """
    spec = SETTINGS_SCHEMA.get(key)
    if spec is None:
        raise ValueError(f"unknown setting: {key}")
    if spec.get("readonly"):
        raise ValueError(f"read-only setting: {key}")
    coerced = _coerce(spec, value)
    cfg = load()
    node = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = coerced
    _legacy_fold(cfg)
    config.save(cfg)
    return cfg


# ── migration ─────────────────────────────────────────────────────────────
def migrate() -> dict:
    """One-time idempotent config migration; returns the final config.

    * flattens ``alerts.thresholds.disk/latency_ms`` → flat keys,
    * ensures every non-readonly schema key exists (fills defaults),
    * preserves unknown/extra keys, never drops user data.
    Only writes the file when something actually changed.
    """
    p = config.config_path()
    if not p.exists():
        cfg = load()
        config.save(cfg)
        return cfg
    try:
        raw = config.parse_yaml(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[settings] migrate: config unreadable ({e}); rewriting with defaults", file=sys.stderr)
        raw = {}
    changed = False

    # 1. fold legacy alerts.thresholds
    alerts = raw.get("alerts")
    if isinstance(alerts, dict):
        thr = alerts.get("thresholds")
        if isinstance(thr, dict):
            if "threshold_disk" not in alerts and isinstance(thr.get("disk"), (int, float)):
                alerts["threshold_disk"] = int(thr["disk"])
                changed = True
            if "latency_ms" not in alerts and isinstance(thr.get("latency_ms"), (int, float)):
                alerts["latency_ms"] = int(thr["latency_ms"])
                changed = True
            if "thresholds" in alerts:
                alerts.pop("thresholds", None)
                changed = True

    # 2. ensure every non-readonly key exists
    for key, spec in SETTINGS_SCHEMA.items():
        if spec.get("readonly"):
            continue
        node = raw
        parts = key.split(".")
        missing = False
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                missing = True
                break
            node = node[part]
        if missing:
            # build the nested path with the default
            node = raw
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = deepcopy(spec["default"])
            changed = True

    if changed:
        config.save(raw)
    return raw


# ── export / import ───────────────────────────────────────────────────────
def export_yaml(include_secrets: bool = False) -> str:
    """Serialize the full normalized config as YAML.

    Secret values are masked unless ``include_secrets`` is True.
    """
    cfg = load()
    if not include_secrets:
        cfg = mask_secrets(cfg)
    return config.dump_yaml(cfg) + "\n"


def import_yaml(text: str) -> dict:
    """Parse + validate a YAML settings import.

    Unknown keys are rejected; bad values raise ValueError with a clear
    message. Returns the new config on success (already saved).
    """
    parsed = config.parse_yaml(text)
    if not isinstance(parsed, dict):
        raise ValueError("import must be a YAML mapping")
    # validate every schema key present in the payload
    for key, spec in SETTINGS_SCHEMA.items():
        if spec.get("readonly"):
            continue
        node = parsed
        parts = key.split(".")
        present = True
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                present = False
                break
            node = node[part]
        if present:
            _coerce(spec, node)  # raises on mismatch
    # unknown top-level keys? reject clearly
    known_prefixes = tuple(k.split(".")[0] for k in SETTINGS_SCHEMA)
    for top in parsed:
        if top not in known_prefixes and top not in ("version", "hermes", "claude"):
            raise ValueError(f"unknown settings group in import: {top}")
    config.save(parsed)
    return parsed


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="settings inspector")
    ap.add_argument("key", nargs="?", default=None)
    args = ap.parse_args()
    if args.key:
        print(get(args.key))
    else:
        print(export_yaml(include_secrets=False))