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


# ── v1.4 handlers ─────────────────────────────────────────────────────────
def _cmd_routing(args):
    """routing | routing list | routing show <phrase> | routing set <cat> <h> | routing add <cat> <h>."""
    from . import routing
    lines = []
    if not args or args[0] == "list":
        for cat in routing.categories():
            lines.append(f"  {cat:<14} -> {routing.get(cat)}")
        lines.append(f"  {'default':<14} -> {settings.get('routing.default', 'auto')}")
        return lines
    if args[0] == "show" and len(args) == 2:
        d = routing.dispatch(args[1])
        return [f"  {args[1]!r} -> {d['harness']} ({d.get('category')}, by={d.get('by')})"]
    if args[0] == "set" and len(args) == 3:
        try:
            routing.set(args[1], args[2])
        except ValueError as e:
            return [f"rejected: {e}"]
        return [f"  {args[1]} -> {routing.get(args[1])}"]
    if args[0] == "add" and len(args) == 3:
        try:
            routing.add(args[1], harness=args[2])
        except ValueError as e:
            return [f"rejected: {e}"]
        return [f"  category added: {args[1]} -> {routing.get(args[1])}"]
    return ["usage: routing [list|show <phrase>|set <cat> <harness>|add <cat> <harness>]"]


def _cmd_mcp(args):
    """mcp | mcp list | mcp add <name> <type> <cmd|url> | remove|enable|disable <name> | rescan | adopt."""
    from . import mcp
    lines = []
    if not args or args[0] == "list":
        entries = mcp.list_servers()
        if not entries:
            return ["  no MCP servers registered (run `mcp rescan`)"]
        for e in entries:
            state = "on" if e["enabled"] else "off"
            lines.append(f"  [{state}] {e['name']:<20} {e['type']:<6} {e['source']:<7} "
                         f"{e.get('mode', 'shared')} {'adopted' if e.get('adopted') else 'pending'}")
        return lines
    if args[0] == "rescan":
        res = mcp.rescan()
        lines.append(f"  rescan: found={len(res.get('found', []))} added={len(res.get('added', []))}")
        for e in res.get("added", []):
            lines.append(f"    + {e['name']} (source: {e['source']})")
        return lines
    if args[0] == "adopt":
        if len(args) == 1:
            res = mcp.adopt("all")
        else:
            res = mcp.adopt(args[1])
        return [f"  adopted: {res}"]
    if args[0] == "add" and len(args) >= 3:
        name, type_ = args[1], args[2]
        if not valid_name(name):
            return [f"invalid server name: {name!r}"]
        try:
            if type_ == "http":
                mcp.add(name, "http", url=args[3] if len(args) > 3 else "")
            else:
                mcp.add(name, "stdio", args[3] if len(args) > 3 else "")
        except ValueError as e:
            return [f"rejected: {e}"]
        return [f"  server added: {name}"]
    if args[0] in ("remove", "enable", "disable", "test", "mode") and len(args) >= 2:
        name = args[1]
        try:
            if args[0] == "remove":
                mcp.remove(name)
            elif args[0] == "enable":
                mcp.enable(name)
            elif args[0] == "disable":
                mcp.disable(name)
            elif args[0] == "test":
                return [f"  {name}: {mcp.status(name)}"]
            else:
                mcp.mode(name, args[2] if len(args) > 2 else "shared")
        except (FileNotFoundError, ValueError) as e:
            return [str(e)]
        return [f"  {args[0]}d {name}"]
    return ["usage: mcp [list|add <name> <type> <cmd|url>|remove|enable|disable <name>|rescan|adopt]"
            if False else "usage: mcp [list|add <name> <type> <cmd|url>|remove <name>|enable <name>"
                          "|disable <name>|rescan|adopt [name]]"]


def _cmd_memory(args):
    """memory | memory add <text> | memory search <q> | memory list | memory stats."""
    from . import memory
    if not args or args[0] == "list":
        notes = memory.list()
        if not notes:
            return ["  no memory notes yet"]
        return [f"  {n['id'][:8]}  {n['text'][:80]}" for n in notes]
    if args[0] == "add" and len(args) >= 2:
        note_id = memory.add(" ".join(args[1:]))
        return [f"  note {note_id[:8]} added"]
    if args[0] == "search" and len(args) >= 2:
        hits = memory.search(" ".join(args[1:]))
        if not hits:
            return ["  no matches"]
        return [f"  {h['id'][:8]}  {h['text'][:80]}" for h in hits]
    if args[0] == "stats":
        return [f"  {memory.stats()}"]
    return ["usage: memory [add <text>|search <q>|list|stats]"]


def _cmd_identity(args):
    """identity | identity list | identity mode <file> <mode> | identity sync <file> | identity restore <file> <n>."""
    from . import identity
    if not args or args[0] == "list":
        files = identity.list_files()
        if not files:
            return ["  no identity files yet"]
        return [f"  {f['name']:<16} {f.get('mode', 'shared'):<12} {f.get('size', 0):>8}B"
                f"  consumed: {', '.join(f.get('consumed_by', []))}" for f in files]
    if args[0] == "mode" and len(args) == 3:
        try:
            identity.mode(args[1], args[2])
        except ValueError as e:
            return [f"rejected: {e}"]
        return [f"  {args[1]} -> {args[2]}"]
    if args[0] == "sync" and len(args) == 2:
        try:
            res = identity.sync(args[1])
            return [f"  synced: {args[1]} {res}"]
        except ValueError as e:
            return [f"rejected: {e}"]
    if args[0] == "restore" and len(args) == 3:
        try:
            identity.restore(args[1], int(args[2]))
        except (ValueError, FileNotFoundError) as e:
            return [str(e)]
        return [f"  restored {args[1]} from snapshot {args[2]}"]
    return ["usage: identity [list|mode <file> <mode>|sync <file>|restore <file> <n>]"]


def _cmd_configs(args):
    """configs | configs list | configs show <name> | configs mode <name> <mode> | configs validate <name>."""
    from . import conflayer
    if not args or args[0] == "list":
        configs = conflayer.list_configs()
        return [f"  {c['name']:<20} {c.get('mode', 'separate'):<10} "
                f"{'exists' if c.get('exists') else 'missing'}" for c in configs]
    if args[0] == "show" and len(args) == 2:
        try:
            content = conflayer.show(args[1])
        except FileNotFoundError as e:
            return [str(e)]
        return [line for line in content.splitlines()[:40]]
    if args[0] == "mode" and len(args) == 3:
        try:
            conflayer.mode(args[1], args[2])
        except ValueError as e:
            return [f"rejected: {e}"]
        return [f"  {args[1]} -> {args[2]}"]
    if args[0] == "validate" and len(args) == 2:
        res = conflayer.validate(args[1])
        if res.get("ok"):
            return [f"  {args[1]}: valid"]
        return [f"  {args[1]}: {e.get('msg', '?')}" for e in res.get("errors", [])]
    return ["usage: configs [list|show <name>|mode <name> <mode>|validate <name>]"]


def _cmd_audit(args):
    """audit | audit summary."""
    from . import audit
    if args and args[0] == "summary":
        s = audit.summary()
        return [f"  canonical={s.get('canonical', 0)} monitored={s.get('monitored', 0)} "
                f"ignored={s.get('ignored', 0)} total={s.get('total', 0)}"]
    rows = audit.table()[:24]
    return [f"  {r['resource']:<16} {r.get('atropos_status', ''):<12} {r.get('recommendation', '')}"
            for r in rows]


def _cmd_fleet(args):
    """fleet | fleet list | fleet ping [name]."""
    from . import fleet
    if not args or args[0] == "list":
        boxes = fleet.list_boxes()
        if not boxes:
            return ["  no fleet boxes registered"]
        return [f"  {b['name']:<16} {b['url']}" for b in boxes]
    if args[0] == "ping":
        rows = fleet.ping(args[1] if len(args) > 1 else "all")
        return [f"  [{('OK' if r.get('ok') else 'FAIL'):<4}] {r['name']:<16} "
                f"{r.get('latency_ms', '—')}ms — {r.get('error') or r.get('version', '')}" for r in rows]
    return ["usage: fleet [list|ping [name]]"]


def _cmd_budget(args):
    """budget | budget usage."""
    from . import budget
    u = budget.usage()
    pct = f"{u.get('pct', 0):.0f}%" if u.get("budget") else "unlimited"
    lines = [f"  total: {u.get('total', 0):,} tokens  budget: {u.get('budget', 0):,} ({pct})"
             f"  over: {u.get('over', False)}"]
    for r, toks in u.get("per_router", {}).items():
        lines.append(f"    {r:<10} {toks:,}")
    return lines


def _cmd_links(args):
    """links | links list | links create <session> | links revoke <token>."""
    from . import links
    if not args or args[0] == "list":
        items = links.list_links()
        if not items:
            return ["  no share links"]
        return [f"  {l['token'][:8]}...  session={l['session_id']}  expires={l['expires']}  used={l['used']}"
                for l in items]
    if args[0] == "create" and len(args) == 2:
        l = links.create(args[1])
        return [f"  share link: {l['url']} (expires {l['expires']})"]
    if args[0] == "revoke" and len(args) == 2:
        links.revoke(args[1])
        return [f"  revoked"]
    return ["usage: links [list|create <session>|revoke <token>]"]


def _cmd_snapshots(args):
    """snapshots | snapshots list | snapshots create [label] | snapshots restore <name>."""
    from . import snapshots
    if not args or args[0] == "list":
        items = snapshots.list_snapshots()
        if not items:
            return ["  no snapshots yet"]
        return [f"  {s['name']:<44} {s.get('size_mb', 0):>6}MB  {s.get('label', '')}" for s in items]
    if args[0] == "create":
        res = snapshots.create(args[1] if len(args) > 1 else "manual")
        return [f"  snapshot: {res.get('path')}"]
    if args[0] == "restore" and len(args) == 2:
        res = snapshots.restore(args[1])
        return [f"  restored: {res}"]
    return ["usage: snapshots [list|create [label]|restore <name>]"]


def _cmd_activity(args):
    """activity — 24h timeline."""
    from . import activity
    feed = activity.feed()
    lines = [f"  updates={feed.get('updates', 0)} alerts={feed.get('alerts', 0)} "
             f"backups={feed.get('backups', 0)} jailbreaks={feed.get('jailbreaks', 0)} "
             f"sessions={feed.get('sessions', 0)} routers={feed.get('routers', 0)}"]
    for e in feed.get("events", [])[:20]:
        lines.append(f"  {e['ts']}  {e['event']:<14} {e.get('detail', '')}")
    return lines


def _cmd_announce(args):
    """announce — tips + changelog + version check."""
    from . import notify
    return [f"  [{i['type']}] {i['text']}" for i in notify.feed()]


def _cmd_files(args):
    """files | files list [path] | files read <path> | files search <q>."""
    from . import files
    if not args or args[0] == "list":
        res = files.list_dir(args[1] if len(args) > 1 else None)
        if not res.get("ok"):
            return [f"  error: {res.get('error', '?')}"]
        return [f"  [{'d' if e['type'] == 'dir' else ' '}] {e['name']:<40} {e.get('size', ''):>10}"
                for e in res["entries"][:40]]
    if args[0] == "read" and len(args) == 2:
        res = files.read_file(args[1])
        if not res.get("ok"):
            return [f"  error: {res.get('error', '?')}"]
        return res["content"].splitlines()[:40]
    if args[0] == "search" and len(args) >= 2:
        res = files.search(q=" ".join(args[1:]))
        hits = res if isinstance(res, list) else res.get("results", [])
        return [f"  {p}" for p in hits[:30]] or ["  no matches"]
    return ["usage: files [list [path]|read <path>|search <q>]"]


def _cmd_chat(args):
    """chat | chat sessions | chat send <text> | chat export <session>."""
    from . import chat
    if not args or args[0] == "sessions":
        sessions = chat.session_list()
        if not sessions:
            return ["  no chat sessions yet"]
        return [f"  {s['id'][:8]}  {s['title']:<30} {s.get('message_count', 0)} msgs"
                for s in sessions]
    if args[0] == "send" and len(args) >= 2:
        res = chat.send(None, " ".join(args[1:]))
        if res.get("ok"):
            return [res["reply"]]
        return [f"  error: {res.get('error', '?')}"]
    if args[0] == "export" and len(args) == 2:
        try:
            return chat.export(args[1]).splitlines()[:40]
        except FileNotFoundError as e:
            return [str(e)]
    return ["usage: chat [sessions|send <text>|export <session>]"]


# ── v1.4 validators ───────────────────────────────────────────────────────
def _validate_routing(args):
    if not args or args[0] == "list":
        return None if len(args) <= 1 else "usage: routing list"
    if args[0] == "set" and len(args) == 3:
        h = args[2]
        if h in ("clotho", "lachesis", "atropos", "auto", "hermes", "claude", "internal"):
            return None
        return f"unknown harness: {h}"
    if args[0] == "add" and len(args) == 3:
        return None if valid_name(args[1]) else "invalid category name"
    if args[0] == "show" and len(args) == 2:
        return None
    return "usage: routing [list|show <phrase>|set <cat> <harness>|add <cat> <harness>]"


def _validate_mcp(args):
    if not args or args[0] == "list":
        return None if len(args) <= 1 else "usage: mcp list"
    if args[0] == "rescan" or (args[0] == "adopt" and len(args) <= 2):
        return None if args[0] == "adopt" and len(args) == 2 and not valid_name(args[1]) \
            else None
    if args[0] == "add" and len(args) >= 3:
        if not valid_name(args[1]):
            return "invalid server name"
        return None if args[2] in ("stdio", "http") else "type must be stdio|http"
    if args[0] in ("remove", "enable", "disable", "test") and len(args) == 2:
        return None if valid_name(args[1]) else "invalid server name"
    if args[0] == "mode" and len(args) == 3:
        return None if valid_name(args[1]) and args[2] in ("shared", "per-harness", "atropos-only") \
            else "usage: mcp mode <name> <shared|per-harness|atropos-only>"
    return "usage: mcp [list|add <name> <type> <cmd|url>|remove <name>|enable <name>" \
           "|disable <name>|rescan|adopt [name]]"


def _validate_memory(args):
    if not args or args[0] in ("list", "stats"):
        return None if len(args) <= 1 else "usage: memory list"
    if args[0] in ("add", "search") and len(args) >= 2:
        return None
    return "usage: memory [add <text>|search <q>|list|stats]"


def _validate_identity(args):
    if not args or args[0] == "list":
        return None if len(args) <= 1 else "usage: identity list"
    if args[0] == "mode" and len(args) == 3:
        return None if args[2] in ("shared", "separate", "atropos-only") \
            else "mode must be shared|separate|atropos-only"
    if args[0] == "sync" and len(args) == 2:
        return None
    if args[0] == "restore" and len(args) == 3:
        try:
            int(args[2])
            return None
        except ValueError:
            return "snapshot index must be an integer"
    return "usage: identity [list|mode <file> <mode>|sync <file>|restore <file> <n>]"


def _validate_configs(args):
    if not args or args[0] == "list":
        return None if len(args) <= 1 else "usage: configs list"
    if args[0] in ("show", "validate", "sync") and len(args) == 2:
        return None
    if args[0] == "mode" and len(args) == 3:
        return None if args[2] in ("shared", "separate", "atropos-only") \
            else "mode must be shared|separate|atropos-only"
    return "usage: configs [list|show <name>|mode <name> <mode>|validate <name>]"


def _validate_files(args):
    if not args or args[0] == "list":
        return None if len(args) <= 2 else "usage: files list [path]"
    if args[0] == "read" and len(args) == 2:
        return None
    if args[0] == "search" and len(args) >= 2:
        return None
    return "usage: files [list [path]|read <path>|search <q>]"


def _validate_chat(args):
    if not args or args[0] == "sessions":
        return None if len(args) <= 1 else "usage: chat sessions"
    if args[0] in ("send",) and len(args) >= 2:
        return None
    if args[0] == "export" and len(args) == 2:
        return None
    return "usage: chat [sessions|send <text>|export <session>]"


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
    # v1.4 universal resources
    "routing": {"usage": "[list|show <phrase>|set <cat> <harness>|add <cat> <harness>]",
                "handler": _cmd_routing, "validator": _validate_routing},
    "mcp": {"usage": "[list|add <name> <type> <command|url>|remove <name>|enable <name>"
                     "|disable <name>|rescan|adopt [name]]",
            "handler": _cmd_mcp, "validator": _validate_mcp},
    "memory": {"usage": "[add <text>|search <q>|list|stats]",
               "handler": _cmd_memory, "validator": _validate_memory},
    "identity": {"usage": "[list|mode <file> <mode>|sync <file>|restore <file> <n>]",
                 "handler": _cmd_identity, "validator": _validate_identity},
    "configs": {"usage": "[list|show <name>|mode <name> <mode>|validate <name>]",
                "handler": _cmd_configs, "validator": _validate_configs},
    "audit": {"usage": "[summary]", "handler": _cmd_audit,
              "validator": lambda a: None if not a or a == ["summary"]
              else "usage: audit [summary]"},
    "fleet": {"usage": "[list|ping [name]]", "handler": _cmd_fleet,
              "validator": lambda a: None if not a or (a[0] == "ping" and len(a) <= 2)
              else "usage: fleet [list|ping [name]]"},
    "budget": {"usage": "[usage]", "handler": _cmd_budget,
               "validator": lambda a: None if not a or a == ["usage"]
               else "usage: budget [usage]"},
    "links": {"usage": "[list|create <session>|revoke <token>]", "handler": _cmd_links,
              "validator": lambda a: None if not a or a in (["list"],) or
              (a[0] in ("create", "revoke") and len(a) == 2)
              else "usage: links [list|create <session>|revoke <token>]"},
    "snapshots": {"usage": "[list|create [label]|restore <name>]", "handler": _cmd_snapshots,
                  "validator": lambda a: None if not a or a == ["list"] or
                  (a[0] == "create" and len(a) <= 2) or (a[0] == "restore" and len(a) == 2)
                  else "usage: snapshots [list|create [label]|restore <name>]"},
    "activity": {"usage": "", "handler": _cmd_activity,
                 "validator": lambda a: "activity takes no arguments" if a else None},
    "announce": {"usage": "", "handler": _cmd_announce,
                 "validator": lambda a: "announce takes no arguments" if a else None},
    "files": {"usage": "[list [path]|read <path>|search <q>]", "handler": _cmd_files,
              "validator": _validate_files},
    "chat": {"usage": "[sessions|send <text>|export <session>]", "handler": _cmd_chat,
             "validator": _validate_chat},
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