#!/usr/bin/env python3
"""Atropos console — whitelist-only command dispatcher for the dashboard.

The Console tab is a safe REPL: the owner types ONLY whitelisted Atropos
commands and the dispatcher maps each token to a function call. There is
no free-form shell, no ``subprocess`` of user input, no ``os.system`` —
arbitrary execution is impossible by construction.

Command format::

    command [arg...]

Every command declares its own ``(regex, validator)`` for arguments.
Names (skills, plugins, market items) must match the extension identifier
regex (no path traversal). Output is captured as structured lines.

Security properties:
  * first token must be an exact registered command,
  * ``rm -rf /``, ``sh``, ``bash``, ``curl | sh`` → "unknown command",
  * side-effecting runs are serialized through a module lock,
  * history is written to ~/.atropos/console_history.jsonl (plain text).
"""
import json
import re
import shlex
import threading
import time
from pathlib import Path

from . import backup, detect, doctor, router, settings, update
from .extensions import valid_name

HISTORY_FILE = "console_history.jsonl"
MAX_HISTORY = 200

# Serializes side-effecting commands (one at a time).
_run_lock = threading.Lock()

# ── whitelist ─────────────────────────────────────────────────────────────
def _cmd_help(args):
    """List every whitelisted command."""
    lines = ["whitelisted commands:"]
    for name, spec in COMMANDS.items():
        lines.append(f"  {name} {spec['usage']}")
    lines.append("  help")
    return lines


def _cmd_version(args):
    """Show version + sha."""
    from . import dashboard
    v = dashboard.api_version()
    return [f"atropos {v['version']} @ {v.get('sha', '')}"]


def _cmd_status(args):
    """System status overview."""
    from . import dashboard
    st = dashboard.api_status()
    rt = st.get("runtime", {})
    return [
        f"os: {rt.get('os', '?')} / {rt.get('os_release', '')}",
        f"cloud: {rt.get('cloud', 'none')}",
        f"router: {st.get('router', {}).get('active', '?')} → {st.get('router', {}).get('model', '')}",
        f"uptime: {st.get('uptime', 0)}s",
        f"disk: {st.get('disk', {}).get('pct', '?')}% used",
    ]


def _cmd_doctor(args):
    """Run health checks (optionally --fix)."""
    fix = "--fix" in args
    results = doctor.doctor(fix=fix)
    lines = []
    for r in results:
        icon = "✅" if r["ok"] else ("🔧" if r.get("fixed") else "❌")
        suffix = " (fixed)" if r.get("fixed") else ""
        lines.append(f"{icon} {r['name']}: {r['msg']}{suffix}")
    failed = [r for r in results if not r["ok"]]
    lines.append("OK" if not failed else f"{len(failed)} issue(s)")
    return lines


def _cmd_self_heal(args):
    """Doctor → patches → watch pipeline."""
    from . import dashboard
    res = dashboard.api_self_heal()
    lines = []
    for s in res.get("stages", []):
        mark = "✅" if s.get("ok") else "❌"
        lines.append(f"{mark} {s['stage']}: {s.get('msg') or s.get('detail') or s.get('error', '')}")
    lines.append("pipeline OK" if res.get("ok") else "pipeline FAILED")
    return lines


def _cmd_backup(args):
    """backup create | backup list."""
    lines = []
    if args and args[0] == "list":
        for b in backup.list_backups():
            lines.append(f"  {b['name']}  ({b['size_mb']} MB)")
        if not lines:
            lines.append("no backups yet")
        return lines
    result = backup.create()
    if result.get("ok"):
        lines.append(f"backup created: {result['path']} ({result['size_mb']} MB)")
        lines.append(f"items: {', '.join(result['items'])}")
    else:
        lines.append(f"backup failed: {result.get('error', '?')}")
    return lines


def _cmd_update(args):
    """update check | update apply."""
    from . import dashboard
    repo = detect.hermes_agent()
    if not repo:
        return ["hermes-agent not found — nothing to update"]
    if not args or args[0] != "apply":
        res = update.update_check(repo)
        if res.get("up_to_date"):
            return [f"up to date @ {res['head']}"]
        if res.get("ok"):
            return [f"behind: {res['behind']} commits ({res['head']} → {res['remote']})"]
        return [f"check failed: {res.get('error', '?')}"]
    res = update.apply_update(repo)
    if res.get("up_to_date"):
        return [f"up to date @ {res['head']}"]
    lines = []
    if res.get("ok"):
        lines.append(f"update applied: {res.get('prev_head')} → {res['head']}")
    else:
        lines.append(f"update failed: {res.get('error')}")
        if res.get("rolled_back"):
            lines.append("rolled back to previous revision")
    for a in res.get("applied", []):
        lines.append(f"  patch: {a}")
    for e in res.get("errors", []):
        lines.append(f"  error: {e}")
    return lines


def _cmd_route(args):
    """route | route test [name] | route set <name>."""
    lines = []
    if not args:
        r = router.get()
        return [f"active: {r.get('active','?')} · model: {r.get('model','?')} · available: {', '.join(router.available())}"]
    if args[0] == "test":
        names = args[1:] or router.available()
        for n in names:
            r = router.ping(n)
            lat = f"{r['latency_ms']}ms" if r.get("latency_ms") is not None else "—"
            icon = "✅" if r.get("ok") else "❌"
            lines.append(f"{icon} {n}: {lat} — {r.get('error') or r.get('model', '')}")
        return lines
    if args[0] == "set" and len(args) == 2:
        name = args[1]
        if name not in router.available():
            return [f"unknown router: {name}"]
        r = router.set_active(name)
        results = router.apply_all()
        for target, ok, msg in results:
            lines.append(f"{'✅' if ok else '❌'} {target}: {msg}")
        lines.append(f"router → {r['active']} ({r['model']})")
        return lines
    return ["usage: route | route test [name] | route set nain|omni|local"]


def _cmd_skills(args):
    """skills | skills list | skills install <name> | skills remove <name>."""
    from . import extensions, skills as skills_mod
    lines = []
    if not args or args[0] == "list":
        for e in extensions.list_extensions("skill"):
            lines.append(f"  [{'on' if e['enabled'] else 'off'}] {e['source']:<7} {e['name']}")
        if not lines:
            lines.append("no skills installed")
        return lines
    if args[0] == "install" and len(args) == 2:
        name = args[1]
        if not valid_name(name):
            return [f"invalid skill name: {name!r}"]
        root = extensions.hermes_skills_dir()
        src = root / name
        if not src.is_dir():
            # allow installing from the universal store (atropos/skills)
            universal = skills_mod.skills_dir() / name
            if not universal.is_dir():
                return [f"skill not found locally: {name}. Use the Market panel to install from a registry."]
            src = universal
        result = extensions.install(src, name, "skill", "hermes")
        lines.append(f"installed {result['name']} → {result['path']}")
        if settings.get("skills.auto_sync", True):
            try:
                skills_mod.sync_to_hermes()
            except Exception as e:
                lines.append(f"sync skipped: {e}")
        return lines
    if args[0] == "remove" and len(args) == 2:
        name = args[1]
        if not valid_name(name):
            return [f"invalid skill name: {name!r}"]
        try:
            result = extensions.remove(name, "skill", "hermes")
            lines.append(f"removed {name} → trash: {result['trashed']}")
        except (FileNotFoundError, ValueError) as e:
            lines.append(str(e))
        return lines
    if args[0] in ("enable", "disable") and len(args) == 2:
        name = args[1]
        if not valid_name(name):
            return [f"invalid skill name: {name!r}"]
        try:
            if args[0] == "enable":
                extensions.enable(name, "skill", "hermes")
            else:
                extensions.disable(name, "skill", "hermes")
            lines.append(f"{args[0]}d {name}")
        except (FileNotFoundError, ValueError) as e:
            lines.append(str(e))
        return lines
    return ["usage: skills [list|install <name>|remove <name>|enable <name>|disable <name>]"]


def _cmd_plugin(args):
    """plugin | plugin list | plugin enable <name> | plugin disable <name>."""
    from . import extensions
    lines = []
    if not args or args[0] == "list":
        for e in extensions.list_extensions("plugin"):
            lines.append(f"  [{'on' if e['enabled'] else 'off'}] {e['name']}")
        if not lines:
            lines.append("no plugins installed")
        return lines
    if args[0] in ("enable", "disable") and len(args) == 2:
        name = args[1]
        if not valid_name(name):
            return [f"invalid plugin name: {name!r}"]
        try:
            if args[0] == "enable":
                extensions.enable(name, "plugin", "hermes")
            else:
                extensions.disable(name, "plugin", "hermes")
            lines.append(f"{args[0]}d {name}")
        except (FileNotFoundError, ValueError) as e:
            lines.append(str(e))
        return lines
    return ["usage: plugin [list|enable <name>|disable <name>]"]


def _cmd_settings(args):
    """settings | settings get <key> | settings set <key> <value>."""
    lines = []
    if not args:
        groups = {}
        for key, spec in settings.schema().items():
            if spec.get("readonly"):
                continue
            groups.setdefault(spec["group"], []).append(key)
        for gname, keys in groups.items():
            lines.append(f"[{gname}]")
            for key in keys:
                val = settings.get(key)
                if settings.is_secret(key) and val:
                    val = settings.SECRET_MASK
                lines.append(f"  {key} = {val}")
        return lines
    if args[0] == "get" and len(args) == 2:
        key = args[1]
        val = settings.get(key)
        if settings.is_secret(key) and val:
            val = settings.SECRET_MASK
        return [f"{key} = {val}"]
    if args[0] == "set" and len(args) == 3:
        key, value = args[1], args[2]
        try:
            parsed = json.loads(value) if value[0] in "[{\"" else value
        except Exception:
            parsed = value
        try:
            settings.set(key, parsed)
            return [f"{key} = {settings.get(key)}"]
        except ValueError as e:
            raise ValueError(f"rejected: {e}")
    return ["usage: settings [get <key>|set <key> <value>]"]


def _cmd_effort(args):
    """effort get | effort set <tier> [--hermes|--claude|--atropos]."""
    from . import dashboard
    lines = []
    if not args or args[0] == "get":
        res = dashboard.api_effort()
        current = res.get("current", {})
        for h in ("hermes", "claude", "atropos"):
            lines.append(f"  {h}: {current.get(h, 'medium')}")
        return lines
    if args[0] == "set" and len(args) >= 2:
        tier = args[1]
        targets = []
        for flag in ("--hermes", "--claude", "--atropos"):
            if flag in args:
                targets.append(flag[2:])
        if not targets:
            targets = ["hermes", "claude", "atropos"]
        res = dashboard.api_effort_set({"tier": tier, "targets": targets})
        if res.get("ok"):
            return [f"effort → {tier} for {', '.join(res['changed'])}"]
        return [f"rejected: {res.get('error', '?')}"]
    return ["usage: effort [get|set <tier> [--hermes|--claude|--atropos]]"]


# name → {usage, handler, validator (args → error or None)}


def _validate_route(args):
    if not args:
        return None
    if args[0] == "test":
        return None if all(valid_name(a) for a in args[1:]) else "invalid router name"
    if args[0] == "set" and len(args) == 2 and args[1] in router.available():
        return None
    return "usage: route [test [name...]|set nain|omni|local]"


def _validate_skills(args):
    if not args or args[0] == "list":
        return None if len(args) <= 1 else "usage: skills list"
    if args[0] in ("install", "remove", "enable", "disable") and len(args) == 2:
        return None if valid_name(args[1]) else "invalid skill name"
    return "usage: skills [list|install <name>|remove <name>|enable <name>|disable <name>]"


def _validate_plugin(args):
    if not args or args[0] == "list":
        return None if len(args) <= 1 else "usage: plugin list"
    if args[0] in ("enable", "disable") and len(args) == 2:
        return None if valid_name(args[1]) else "invalid plugin name"
    return "usage: plugin [list|enable <name>|disable <name>]"


def _validate_settings(args):
    if not args:
        return None
    if args[0] == "get" and len(args) == 2:
        return None if args[1] in settings.schema() else f"unknown setting: {args[1]}"
    if args[0] == "set" and len(args) == 3:
        return None if args[1] in settings.schema() else f"unknown setting: {args[1]}"
    return "usage: settings [get <key>|set <key> <value>]"


def _validate_effort(args):
    if not args or args[0] == "get":
        return None if len(args) <= 1 else "usage: effort get"
    if args[0] == "set" and len(args) >= 2:
        if args[1] not in settings.EFFORT_TIERS:
            return f"unknown tier: {args[1]}"
        allowed = {"--hermes", "--claude", "--atropos"}
        if any(a not in allowed for a in args[2:]):
            return "usage: effort set <tier> [--hermes|--claude|--atropos]"
        return None
    return "usage: effort [get|set <tier>]"


# name → {usage, handler, validator (args → error or None)}
COMMANDS = {
    "help": {"usage": "", "handler": _cmd_help,
             "validator": lambda a: "help takes no arguments" if a else None},
    "version": {"usage": "", "handler": _cmd_version,
                "validator": lambda a: "version takes no arguments" if a else None},
    "status": {"usage": "", "handler": _cmd_status,
               "validator": lambda a: "status takes no arguments" if a else None},
    "doctor": {"usage": "[--fix]", "handler": _cmd_doctor,
               "validator": lambda a: None if all(x == "--fix" for x in a) and len(a) <= 1
               else "usage: doctor [--fix]"},
    "self-heal": {"usage": "", "handler": _cmd_self_heal,
                  "validator": lambda a: "self-heal takes no arguments" if a else None},
    "backup": {"usage": "create|list", "handler": _cmd_backup,
               "validator": lambda a: None if not a or a in (["create"], ["list"])
               else "usage: backup create|list"},
    "update": {"usage": "check|apply", "handler": _cmd_update,
               "validator": lambda a: None if not a or a in (["check"], ["apply"])
               else "usage: update check|apply"},
    "route": {"usage": "[test|set <name>]", "handler": _cmd_route,
              "validator": _validate_route},
    "skills": {"usage": "[list|install|remove|enable|disable] <name>",
               "handler": _cmd_skills, "validator": _validate_skills},
    "plugin": {"usage": "[list|enable|disable] <name>",
               "handler": _cmd_plugin, "validator": _validate_plugin},
    "settings": {"usage": "[get <key>|set <key> <value>]",
                 "handler": _cmd_settings, "validator": _validate_settings},
    "effort": {"usage": "[get|set <tier>]", "handler": _cmd_effort,
               "validator": _validate_effort},
}


# ── dispatcher ────────────────────────────────────────────────────────────
def validate(line: str):
    """Validate a command line. Returns (command_name, error) or (None, err)."""
    try:
        args = shlex.split(line)
    except ValueError as e:
        return None, f"parse error: {e}"
    if not args:
        return None, "empty command"
    name = args[0]
    spec = COMMANDS.get(name)
    if spec is None:
        return None, f"unknown command: {name} — run `help` for the whitelist"
    err = spec["validator"](args[1:])
    if err:
        return name, err
    return name, None


def run_command(line: str) -> dict:
    """Execute one whitelisted command. Returns
    {ok, command, output: [lines], error?}.

    Serialized via a module lock so side-effecting commands never run
    concurrently.
    """
    name, err = validate(line)
    if err:
        return {"ok": False, "command": line, "error": err, "output": []}
    spec = COMMANDS[name]
    history_append(line)
    with _run_lock:
        try:
            output = spec["handler"](shlex.split(line)[1:])
            return {"ok": True, "command": line, "output": output or []}
        except Exception as e:
            return {"ok": False, "command": line, "error": str(e), "output": []}


# ── history ───────────────────────────────────────────────────────────────
def history_path() -> Path:
    """Console history file (~/.atropos/console_history.jsonl)."""
    return detect.atropos_home() / HISTORY_FILE


def history_append(line: str):
    """Append one command to history (capped)."""
    try:
        p = history_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "command": line,
            }, ensure_ascii=False) + "\n")
        # cap file length
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > MAX_HISTORY:
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines[-MAX_HISTORY:])
    except Exception:
        pass


def history_list(limit: int = 50) -> list:
    """Recent console commands (newest first)."""
    p = history_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out[::-1]


if __name__ == "__main__":
    import sys
    for ln in sys.stdin:
        ln = ln.strip()
        if not ln:
            continue
        result = run_command(ln)
        print(json.dumps(result, ensure_ascii=False, indent=2))