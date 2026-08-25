#!/usr/bin/env python3
"""Atropos audit — complete-picture resource matrix, stdlib only.

Walks the box and reports, for every resource category in the universal
list, where Hermes keeps it, where Claude keeps it, what Atropos currently
does with it (canonical / monitored / ignored) and a recommendation
(manage-in-atropos / monitor / leave).

Discovery is dynamic wherever possible:

  * Hermes  ``$HERMES_HOME`` — skills/, plugins/, hooks/, scripts/, cron/
    (yaml job files), sessions/, .env, config.yaml, state.db, logs/,
    MEMORY.md, assets/ (identity files), webhooks config section.
  * Claude  ``~/.claude`` — skills/, settings.json, commands/, CLAUDE.md,
    mcp.json, keybindings.json, plus ``~/.claude.json``.
  * Atropos ``~/.atropos`` — the canonical stores: mcp_servers.json,
    models.json, webhooks.json, links.json, fleet.json, identity/,
    configs/, memory/, skills/, backups/, config.yaml, runtime.json,
    cron_state.json, watch.log, dashboard_auth.json, alert_state.json, trash/.

``table()`` returns rows ``{resource, hermes, claude, atropos_status,
recommendation}`` sorted by category (the universal list order, then
alphabetical). ``summary()`` returns counts per atropos_status.
``categories()`` lists the universal categories. Rows carry a ``found``
flag (True when the resource is actually present on the box) so consumers
can distinguish "present" from "not installed".

Pure stdlib. Never imports core.dashboard (circular).
"""
import json
from pathlib import Path

from . import config, detect

# ── universal resource categories (order = the universal list) ────────────
# Each category: {resource, hermes, claude, canonical, atropos_status,
# recommendation}. ``canonical`` is the atropos store path template
# ("" when Atropos only monitors or ignores the resource).
CATEGORIES = [
    {
        "resource": "mcp",
        "hermes": "$HERMES_HOME/config.yaml (mcp/mcp_servers section)",
        "claude": "~/.claude/mcp.json + ~/.claude.json (mcpServers)",
        "canonical": "mcp_servers.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "models",
        "hermes": "$HERMES_HOME/config.yaml (model section)",
        "claude": "~/.claude/settings.json (default_model)",
        "canonical": "models.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "commands",
        "hermes": "$HERMES_HOME/commands (universal, synced)",
        "claude": "~/.claude/commands/*.md + ~/.claude/commands/*.yml",
        "canonical": "commands.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "aliases",
        "hermes": "n/a (no alias store)",
        "claude": "~/.claude.json (hashtags/aliases)",
        "canonical": "commands.json (aliases section)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "env",
        "hermes": "$HERMES_HOME/.env",
        "claude": "shell env / ~/.claude/settings.json env block",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "secrets",
        "hermes": "$HERMES_HOME/auth.json + .env + .gh_backup_token",
        "claude": "~/.claude/settings.json (api keys / env)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "templates",
        "hermes": "$HERMES_HOME/assets (persona/setup templates)",
        "claude": "~/.claude/templates",
        "canonical": "templates/ (repo templates/)",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "identity files",
        "hermes": "$HERMES_HOME/MEMORY.md, SOUL.md, USER.md, .first-setup.json",
        "claude": "~/.claude/CLAUDE.md (project/user memory)",
        "canonical": "identity/",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "config files",
        "hermes": "$HERMES_HOME/config.yaml (+ .pc-original/.server-backup)",
        "claude": "~/.claude/settings.json",
        "canonical": "configs/",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "webhooks",
        "hermes": "$HERMES_HOME/config.yaml (webhooks section)",
        "claude": "n/a (no webhook store)",
        "canonical": "webhooks.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "cron",
        "hermes": "$HERMES_HOME/cron/*.yaml (job files)",
        "claude": "n/a (no cron store)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "skills",
        "hermes": "$HERMES_HOME/skills/<name>/SKILL.md",
        "claude": "~/.claude/skills/<name>/SKILL.md",
        "canonical": "skills/ (universal store)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "plugins",
        "hermes": "$HERMES_HOME/plugins/<name>/plugin.yaml",
        "claude": "n/a (no plugin store)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "hooks",
        "hermes": "$HERMES_HOME/hooks/*.py (log channel hook etc.)",
        "claude": "~/.claude/settings.json (hooks section)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "personas",
        "hermes": "$HERMES_HOME/assets/guest_persona.md",
        "claude": "~/.claude/CLAUDE.md (persona via memory)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "effort",
        "hermes": "$HERMES_HOME/config.yaml (effort)",
        "claude": "~/.claude/settings.json (effort)",
        "canonical": "config.yaml (effort section)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "routers",
        "hermes": "$HERMES_HOME/.env (OPENAI_* vars)",
        "claude": "~/.claude/settings.json (router key)",
        "canonical": "config.yaml (router section)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "backups",
        "hermes": "n/a (state.db is the source)",
        "claude": "n/a (no backup store)",
        "canonical": "backups/",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "alerts",
        "hermes": "$HERMES_HOME/config.yaml (telegram token)",
        "claude": "n/a (no alert store)",
        "canonical": "config.yaml (alerts section) + alert_state.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "jailbreak",
        "hermes": "hermes-agent source + $HERMES_HOME/config.yaml",
        "claude": "~/.claude/settings.json (permission mode)",
        "canonical": "config.yaml (jailbreak section)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "sessions",
        "hermes": "$HERMES_HOME/sessions/*.jsonl + state.db (sqlite)",
        "claude": "~/.claude/projects/*/*.jsonl (conversation logs)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "marketplace",
        "hermes": "$HERMES_HOME/plugins (install target)",
        "claude": "~/.claude/skills (install target)",
        "canonical": "config.yaml (market source allowlist)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "fleet",
        "hermes": "n/a (single box)",
        "claude": "n/a (single box)",
        "canonical": "fleet.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "memory",
        "hermes": "$HERMES_HOME/MEMORY.md + state.db",
        "claude": "~/.claude/CLAUDE.md",
        "canonical": "memory/notes.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "themes",
        "hermes": "$HERMES_HOME/assets (theme files)",
        "claude": "~/.claude/settings.json (theme)",
        "canonical": "config.yaml (dashboard.theme)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "i18n",
        "hermes": "$HERMES_HOME/assets (locale files)",
        "claude": "~/.claude/settings.json (locale)",
        "canonical": "config.yaml (dashboard.lang)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "update",
        "hermes": "hermes-agent git repo (head/remote)",
        "claude": "n/a (auto-updates itself)",
        "canonical": "update_state.json",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "logs",
        "hermes": "$HERMES_HOME/logs/*.log + gateway_state.json",
        "claude": "~/.claude/projects (session transcripts)",
        "canonical": "",
        "atropos_status": "monitored",
        "recommendation": "monitor",
    },
    {
        "resource": "permissions",
        "hermes": "$HERMES_HOME/config.yaml (role gates)",
        "claude": "~/.claude/settings.json (permissions section)",
        "canonical": "config.yaml (permissions.preset)",
        "atropos_status": "canonical",
        "recommendation": "manage-in-atropos",
    },
    {
        "resource": "tui",
        "hermes": "n/a",
        "claude": "n/a",
        "canonical": "",
        "atropos_status": "ignored",
        "recommendation": "leave",
    },
]


def categories() -> list:
    """Universal resource category names, in the universal list order."""
    return [c["resource"] for c in CATEGORIES]


def _found(resource: str) -> bool:
    """True when the resource is actually present on this box."""
    h = detect.hermes_home()
    c = detect._home() / ".claude"
    a = detect.atropos_home()
    if resource == "mcp":
        return _json_has(c / "mcp.json", "mcpServers") or _json_has(
            detect._home() / ".claude.json", "mcpServers") or _yaml_has(
            h / "config.yaml", ("mcp", "mcp_servers", "plugins"))
    if resource == "models":
        return (a / "models.json").exists() or (h / "config.yaml").exists()
    if resource == "commands":
        return (a / "commands.json").exists() or (c / "commands").is_dir() \
            or (h / "commands").is_dir()
    if resource == "aliases":
        data = _json_read(a / "commands.json")
        return bool(isinstance(data, dict) and data.get("aliases"))
    if resource == "env":
        return (h / ".env").exists()
    if resource == "secrets":
        return any((h / n).exists() for n in
                   ("auth.json", ".env", ".gh_backup_token"))
    if resource == "templates":
        return (h / "assets").is_dir() or (c / "templates").is_dir()
    if resource == "identity files":
        return any((h / n).exists() for n in
                   ("MEMORY.md", "SOUL.md", "USER.md", ".first-setup.json")) \
            or (c / "CLAUDE.md").exists() or (a / "identity").is_dir()
    if resource == "config files":
        return (h / "config.yaml").exists() or (c / "settings.json").exists() \
            or (a / "configs").is_dir()
    if resource == "webhooks":
        return (a / "webhooks.json").exists() or _yaml_has(
            h / "config.yaml", ("webhooks",))
    if resource == "cron":
        return (h / "cron").is_dir() or (a / "cron_state.json").exists()
    if resource == "skills":
        return (h / "skills").is_dir() or (c / "skills").is_dir() \
            or (a / "skills").is_dir()
    if resource == "plugins":
        return (h / "plugins").is_dir()
    if resource == "hooks":
        return (h / "hooks").is_dir() or _yaml_has(
            c / "settings.json", ("hooks",))
    if resource == "personas":
        return (h / "assets" / "guest_persona.md").exists()
    if resource == "effort":
        return (a / "config.yaml").exists()
    if resource == "routers":
        return (a / "config.yaml").exists() or (h / ".env").exists() \
            or (c / "settings.json").exists()
    if resource == "backups":
        return (a / "backups").is_dir()
    if resource == "alerts":
        return (a / "config.yaml").exists() or (a / "alert_state.json").exists()
    if resource == "jailbreak":
        return (a / "config.yaml").exists() or (c / "settings.json").exists()
    if resource == "sessions":
        return (h / "sessions").is_dir() or (h / "state.db").exists()
    if resource == "marketplace":
        return (a / "config.yaml").exists()
    if resource == "fleet":
        return (a / "fleet.json").exists()
    if resource == "memory":
        return (a / "memory").is_dir() or (h / "MEMORY.md").exists() \
            or (c / "CLAUDE.md").exists()
    if resource == "themes":
        return (a / "config.yaml").exists()
    if resource == "i18n":
        return (a / "config.yaml").exists()
    if resource == "update":
        return (a / "update_state.json").exists()
    if resource == "logs":
        return (h / "logs").is_dir() or (a / "watch.log").exists()
    if resource == "permissions":
        return (a / "config.yaml").exists() or (c / "settings.json").exists()
    if resource == "tui":
        return (a / "console_history.jsonl").exists()
    return False


def _json_read(p: Path):
    """Best-effort JSON read; None on missing/corrupt."""
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _json_has(p: Path, key: str) -> bool:
    """True when a JSON file exists and carries a non-empty ``key``."""
    data = _json_read(p)
    return bool(isinstance(data, dict) and data.get(key))


def _yaml_has(p: Path, keys: tuple) -> bool:
    """True when a YAML file exists and carries any of ``keys``."""
    if not p.exists():
        return False
    try:
        data = config.parse_yaml(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(isinstance(data, dict) and any(k in data for k in keys))


def table() -> list:
    """Full resource matrix, sorted by category then name.

    Each row: {resource, hermes, claude, atropos_status, recommendation,
    found}. ``atropos_status`` is one of canonical / monitored / ignored.
    """
    rows = []
    for c in CATEGORIES:
        rows.append({
            "resource": c["resource"],
            "hermes": c["hermes"],
            "claude": c["claude"],
            "atropos_status": c["atropos_status"],
            "recommendation": c["recommendation"],
            "found": _found(c["resource"]),
        })
    return sorted(rows, key=lambda r: (r["resource"], r["atropos_status"]))


def summary() -> dict:
    """Counts per atropos_status over the full matrix.

    Returns {total, canonical, monitored, ignored, found, missing} where
    ``found``/``missing`` count categories actually present on the box.
    """
    rows = table()
    counts = {"canonical": 0, "monitored": 0, "ignored": 0}
    found = 0
    for r in rows:
        if r["atropos_status"] in counts:
            counts[r["atropos_status"]] += 1
        if r["found"]:
            found += 1
    return {
        "total": len(rows),
        "canonical": counts["canonical"],
        "monitored": counts["monitored"],
        "ignored": counts["ignored"],
        "found": found,
        "missing": len(rows) - found,
    }


if __name__ == "__main__":
    for r in table():
        mark = "•" if r["found"] else " "
        print(f"{mark} {r['resource']:<16} {r['atropos_status']:<10} "
              f"{r['recommendation']}")
    print()
    print(summary())
