#!/usr/bin/env python3
"""Atropos TUI — terminal user interface like Claude Code.

Runs `atropos tui` → interactive menu-driven terminal UI with:
- Colored boxes, status indicators, quick actions
- Keyboard navigation (arrows + enter, or 1-9 keys)
- Live status refresh (ESC to exit)

Pure stdlib (ANSI escape codes). Works on any terminal.
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime

from . import config, detect, doctor, patches, router, settings
from . import fleet
from .backup import create as backup_create, list_backups
from .watch import run_watch

HISTORY_FILE = "tui_history.json"

# ── ANSI colors ────────────────────────────────────────────────────────────
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_CYAN = "\033[36m"
C_WHITE = "\033[37m"
C_BG_DARK = "\033[48;5;236m"
C_BG_GREEN = "\033[48;5;22m"
C_BG_RED = "\033[48;5;52m"
C_BG_BLUE = "\033[48;5;17m"

CLEAR = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _cols() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def _rows() -> int:
    try:
        return shutil.get_terminal_size().lines
    except Exception:
        return 24


def _bar(inner: str, width: int, color: str = C_CYAN) -> str:
    """Draw a boxed line: ┌─ {inner} ─┐"""
    pad = max(1, width - len(inner) - 4)
    return f"{color}┌─ {C_RESET}{inner}{color} {'─' * pad}┐{C_RESET}"


def _line(width: int, color: str = C_CYAN) -> str:
    return f"{color}└{'─' * (width - 2)}┘{C_RESET}"


def _center(text: str, width: int) -> str:
    if len(text) >= width:
        return text[: width - 1]
    pad = (width - len(text)) // 2
    return " " * pad + text


def _status_icon(ok: bool) -> str:
    return f"{C_GREEN}●{C_RESET}" if ok else f"{C_RED}●{C_RESET}"


def header(title: str, subtitle: str = "") -> int:
    w = min(_cols(), 100)
    print(f"{CLEAR}{C_BG_DARK}")
    print(_center(f"  {C_BOLD}{C_CYAN}⟁ ATROPOS{C_RESET}  ", w))
    print(_center(f"{C_DIM}{title}{C_RESET}", w))
    if subtitle:
        print(_center(f"{C_DIM}{subtitle}{C_RESET}", w))
    print(f"{C_RESET}")
    return w


def footer(w: int, hint: str = "↑↓ navigate · Enter select · q/ESC quit"):
    print(f"{C_DIM}{'─' * w}{C_RESET}")
    print(f"{C_DIM}{hint}{C_RESET}")


def render_status_panel() -> None:
    """Main status screen."""
    w = header("System Status", "live view — press any key to refresh")
    print()
    r = detect.detect()
    cfg = config.load()

    rows = [
        ("OS", f"{r['os']} / {r.get('os_release','')}"),
        ("Python", r.get("python_version", "?")),
        ("Cloud", r.get("cloud", "none")),
        ("Hermes home", r.get("hermes_home", "?")),
        ("Hermes agent", r.get("hermes_agent", "not found")),
        ("Claude", r.get("claude", "not found")),
        ("PTB", r.get("ptb_version", "?")),
        ("Router", f"{cfg.get('router',{}).get('active','?')} → {cfg.get('router',{}).get('model','?')}"),
        ("Effort", str(cfg.get("effort", {}).get("hermes", "medium"))),
        ("Guest", "enabled" if cfg.get("guest", {}).get("enabled") else "disabled"),
    ]

    print(f"  {C_BOLD}{C_WHITE}Runtime{C_RESET}")
    for k, v in rows:
        print(f"    {C_CYAN}{k:<14}{C_RESET} {v}")

    # doctor results
    print(f"\n  {C_BOLD}{C_WHITE}Health{C_RESET}")
    results = doctor.doctor(fix=False)
    for r in results:
        icon = _status_icon(r["ok"])
        color = C_GREEN if r["ok"] else C_RED
        print(f"    {icon} {color}{r['name']:<24}{C_RESET} {r['msg']}")

    # disk
    try:
        usage = shutil.disk_usage(r["hermes_home"])
        pct = usage.used / usage.total * 100
        bar_w = 30
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        color = C_GREEN if pct < 70 else (C_YELLOW if pct < 85 else C_RED)
        print(f"\n  {C_BOLD}{C_WHITE}Disk{C_RESET} {color}{pct:.0f}%{C_RESET}")
        print(f"    {color}{bar}{C_RESET}  {usage.free/1e6:.0f}MB free / {usage.total/1e6:.0f}MB")
    except Exception:
        pass

    # router cards
    print(f"\n  {C_BOLD}{C_WHITE}Routers{C_RESET}")
    active = cfg.get("router", {}).get("active", "")
    for name in router.available():
        mark = f"{C_GREEN}◉{C_RESET}" if name == active else f"{C_DIM}○{C_RESET}"
        rcfg = router.ROUTERS.get(name, {})
        print(f"    {mark} {C_BOLD}{name:<8}{C_RESET} {C_DIM}{rcfg.get('description','')}{C_RESET}")

    # patches
    results = patches.verify()
    ok_p = sum(1 for r in results if r["applied"])
    print(f"\n  {C_BOLD}{C_WHITE}Patches{C_RESET} {ok_p}/{len(results)} applied")
    for r in results[:6]:
        print(f"    {_status_icon(r['applied'])} {r['id']}")
    if len(results) > 6:
        print(f"    {C_DIM}... +{len(results)-6} more{C_RESET}")

    footer(w, "q/ESC quit · d doctor · p patches · r router · b backup · w watch")


def render_doctor(w: int) -> None:
    header("Doctor", "health checks")
    results = doctor.doctor(fix=False)
    for r in results:
        icon = _status_icon(r["ok"])
        color = C_GREEN if r["ok"] else C_RED
        print(f"  {icon} {color}{r['name']:<24}{C_RESET} {r['msg']}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Run Doctor   {C_BOLD}[2]{C_RESET} Apply All Fixes   {C_BOLD}[3]{C_RESET} Back")
    footer(w)


def render_patches(w: int) -> None:
    header("Patches", f"{len(patches.load_hacks())} hacks")
    results = patches.verify()
    for r in results:
        icon = _status_icon(r["applied"])
        color = C_GREEN if r["applied"] else C_RED
        print(f"  {icon} {color}{r['id']:<40}{C_RESET} {r.get('target','')}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Apply All   {C_BOLD}[2]{C_RESET} Verify   {C_BOLD}[3]{C_RESET} Back")
    footer(w)


def render_routers(w: int) -> None:
    header("Routers", "nain | omni | local")
    cfg = config.load()
    active = cfg.get("router", {}).get("active", "")
    for i, name in enumerate(router.available(), 1):
        rcfg = router.ROUTERS.get(name, {})
        mark = f"{C_GREEN}◉{C_RESET}" if name == active else f"{C_DIM}○{C_RESET}"
        print(f"  {C_BOLD}[{i}]{C_RESET} {mark} {C_BOLD}{name:<8}{C_RESET} {C_DIM}{rcfg.get('description','')}{C_RESET}")
    print()
    print(f"  {C_BOLD}[4]{C_RESET} Back")
    footer(w)


def render_backup(w: int) -> None:
    header("Backup", "full state backup")
    backups = list_backups()
    if backups:
        for b in backups[:8]:
            print(f"  {C_CYAN}▸{C_RESET} {b['name']}  ({b['size_mb']} MB)")
    else:
        print("  (no backups yet)")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Create Backup   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_watch(w: int) -> None:
    header("Watch", "self-healing checks")
    results = run_watch()
    print(f"  {C_BOLD}{C_WHITE}Alerts{C_RESET}")
    if results["alerts"]:
        for a in results["alerts"]:
            print(f"  {C_RED}⚠ {a}{C_RESET}")
    else:
        print(f"  {C_GREEN}✓ All checks passed{C_RESET}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Refresh   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def _history_path():
    return detect.atropos_home() / HISTORY_FILE


def _history_load() -> list:
    """Last visited panels, oldest → newest (max 12)."""
    try:
        p = _history_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(x) for x in data]
    except Exception:
        pass
    return []


def _history_push(panel: str):
    """Record a visited panel."""
    try:
        hist = _history_load()
        hist = [p for p in hist if p != panel]
        hist.append(panel)
        hist = hist[-12:]
        p = _history_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


TUI_PANELS = [
    ("s", "status", "Status", "live system overview"),
    ("d", "doctor", "Doctor", "health checks & fixes"),
    ("p", "patches", "Patches", "apply/verify hacks"),
    ("r", "routers", "Routers", "switch nain/omni/local"),
    ("b", "backup", "Backup", "create/list backups"),
    ("w", "watch", "Watch", "run self-healing checks"),
    ("c", "config", "Config", "show config"),
    ("e", "effort", "Effort", "per-harness effort tiers"),
    ("m", "extensions", "Extensions", "skills & plugins"),
    ("F", "fleet", "Fleet", "multi-box health"),
    ("I", "identity", "Identity", "SOUL/AGENTS/SYSTEM modes"),
    ("C", "configs", "Configs", "universal config manager"),
    ("M", "memory", "Memory", "RAG notes + search"),
    ("A", "audit", "Audit", "resource matrix"),
    ("B", "budget", "Budget", "usage & quota"),
    ("S", "snapshots", "Snapshots", "gallery + restore"),
    ("T", "activity", "Activity", "24h timeline"),
    ("q", "quit", "Quit", "exit"),
]


def render_extensions(w: int) -> None:
    """Extensions panel: unified skills + plugins listing."""
    from . import extensions
    header("Extensions", "skills (hermes + claude) · plugins")
    items = extensions.list_extensions()
    if not items:
        print("  (no extensions installed)")
    else:
        for e in items[:24]:
            state = f"{C_GREEN}on{C_RESET}" if e["enabled"] else f"{C_DIM}off{C_RESET}"
            print(f"  [{state}] {e['kind']:<7} {e['source']:<7} {C_BOLD}{e['name']}{C_RESET}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Skills   {C_BOLD}[2]{C_RESET} Plugins   {C_BOLD}[3]{C_RESET} Back")
    footer(w)


def render_fleet(w: int) -> None:
    """Fleet panel: multi-box health."""
    from . import fleet
    header("Fleet", "multi-box health grid")
    boxes = fleet.list_boxes()
    if not boxes:
        print("  (no fleet boxes registered)")
    else:
        for b in boxes[:12]:
            st = b.get("last_status", {})
            lat = st.get("latency_ms")
            ok = st.get("ok")
            icon = C_GREEN + "OK" + C_RESET if ok else (C_RED + "FAIL" + C_RESET if ok is False else C_DIM + "—" + C_RESET)
            print(f"  {icon} {C_BOLD}{b['name']:<16}{C_RESET} {b['url']:<40} {lat if lat is not None else '—'}ms")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Ping all   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_identity(w: int) -> None:
    """Identity panel: universal identity files + modes."""
    from . import identity
    header("Identity", "SOUL / AGENTS / SYSTEM / GUEST / CODE_STYLE")
    files = identity.list_files()
    if not files:
        print("  (no identity files yet)")
    else:
        for f in files[:20]:
            mode = f.get("mode", "shared")
            mode_icon = {"shared": "S", "separate": "H", "atropos-only": "A+🔒"}.get(mode, mode)
            print(f"  [{mode_icon}] {C_BOLD}{f['name']:<16}{C_RESET} {f.get('size', 0):>8}B"
                  f"  {C_DIM}{', '.join(f.get('consumed_by', []))}{C_RESET}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Sync all shared   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_configs(w: int) -> None:
    """Configs panel: universal config manager."""
    from . import conflayer
    header("Configs", "hermes / claude / router / atropos")
    configs = conflayer.list_configs()
    for c in configs[:16]:
        state = "exists" if c.get("exists") else C_DIM + "missing" + C_RESET
        print(f"  {c['mode']:<10} {C_BOLD}{c['name']:<22}{C_RESET} {state}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Validate all   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_memory(w: int) -> None:
    """Memory panel: RAG notes."""
    from . import memory
    header("Memory", "notes + keyword search")
    notes = memory.list()[:20]
    if not notes:
        print("  (no memory notes yet)")
    else:
        for n in notes:
            print(f"  {C_CYAN}{n['id'][:8]}{C_RESET}  {n['text'][:70]}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Stats   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_audit(w: int) -> None:
    """Audit panel: complete-picture resource matrix."""
    from . import audit
    header("Audit", "resource -> hermes / claude / atropos")
    rows = audit.table()
    stats = audit.summary()
    print(f"  {C_DIM}canonical={stats.get('canonical', 0)} monitored={stats.get('monitored', 0)} "
          f"ignored={stats.get('ignored', 0)} total={stats.get('total', 0)}{C_RESET}")
    for r in rows[:22]:
        status = r.get("atropos_status", "")
        color = C_GREEN if status == "canonical" else (C_YELLOW if status == "monitored" else C_DIM)
        print(f"  {color}{r['resource']:<18}{C_RESET} {status:<10} {r.get('recommendation', '')}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Back")
    footer(w)


def render_budget(w: int) -> None:
    """Budget panel: usage & quota gate."""
    from . import budget
    header("Budget", "per-router token usage")
    u = budget.usage()
    pct = f"{u.get('pct', 0):.0f}%" if u.get("budget") else "unlimited"
    print(f"  total: {u.get('total', 0):,} tokens  budget: {u.get('budget', 0):,} ({pct})"
          f"  over: {C_RED if u.get('over') else C_GREEN}{u.get('over', False)}{C_RESET}")
    for r, toks in u.get("per_router", {}).items():
        print(f"    {C_CYAN}{r:<10}{C_RESET} {toks:,}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Check now (alert/failover)   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_snapshots(w: int) -> None:
    """Snapshots panel: gallery + restore."""
    from . import snapshots
    header("Snapshots", "config + identity + settings")
    items = snapshots.list_snapshots()
    if not items:
        print("  (no snapshots yet)")
    else:
        for s in items[:10]:
            print(f"  {C_CYAN}{s['name'][:42]}{C_RESET} {s.get('size_mb', 0):>6}MB  {s.get('label', '')}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Create snapshot   {C_BOLD}[2]{C_RESET} Back")
    footer(w)


def render_activity(w: int) -> None:
    """Activity panel: 24h timeline."""
    from . import activity
    header("Activity", "what happened on this box")
    feed = activity.feed()
    print(f"  updates={feed.get('updates', 0)}  alerts={feed.get('alerts', 0)}  "
          f"backups={feed.get('backups', 0)}  jailbreaks={feed.get('jailbreaks', 0)}  "
          f"sessions={feed.get('sessions', 0)}  routers={feed.get('routers', 0)}")
    for e in feed.get("events", [])[:20]:
        print(f"  {C_DIM}{e['ts'][:19]}{C_RESET} {C_CYAN}{e['event']:<14}{C_RESET} {e.get('detail', '')[:50]}")
    print()
    print(f"  {C_BOLD}[1]{C_RESET} Back")
    footer(w)


def run() -> None:
    """Interactive TUI main loop (arrow keys + letters + history)."""
    if not _is_tty():
        print("tui requires a TTY. Try `atropos status` instead.")
        return
    if os.name == "nt":
        print("TUI requires a POSIX terminal (termios). Try `atropos status` instead.")
        return

    print(f"{HIDE_CURSOR}", end="")
    sel = 0
    try:
        w = _cols()
        while True:
            # main menu
            header("Command Center", "choose a panel (↑↓ + Enter)")
            print()
            for idx, (key, pid, name, desc) in enumerate(TUI_PANELS):
                color = C_RED if pid == "quit" else C_CYAN
                mark = f"{C_GREEN}›{C_RESET}" if idx == sel else " "
                print(f"  {mark} {C_BOLD}{color}{key}{C_RESET}  {C_BOLD}{name:<12}{C_RESET} {C_DIM}{desc}{C_RESET}")
            footer(w, "↑↓ navigate · Enter select · q/ESC quit · 1-9 quick open")

            # read key with arrow support
            key = _read_key()
            if key == "\x1b[A":      # up
                sel = (sel - 1) % len(TUI_PANELS)
                continue
            elif key == "\x1b[B":    # down
                sel = (sel + 1) % len(TUI_PANELS)
                continue
            elif key in ("q", "\x1b"):
                print(f"{SHOW_CURSOR}")
                print(f"{C_GREEN}Bye{C_RESET}")
                break
            elif key == "\r" or key == "\n":
                key = TUI_PANELS[sel][1]
            elif key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
                idx = int(key) - 1
                if idx < len(TUI_PANELS):
                    key = TUI_PANELS[idx][1]
            # dispatch
            if key in ("s", "status"):
                _history_push("status")
                render_status_panel()
                _wait_key()
            elif key in ("d", "doctor"):
                _history_push("doctor")
                render_doctor(w)
                k2 = _read_key()
                if k2 == "2":  # apply fixes
                    print(f"\n  {C_YELLOW}Applying fixes...{C_RESET}")
                    doctor.doctor(fix=True)
                    print(f"  {C_GREEN}Done. Press any key.{C_RESET}")
                    _wait_key()
            elif key in ("p", "patches"):
                _history_push("patches")
                render_patches(w)
                k2 = _read_key()
                if k2 == "1":  # apply all
                    print(f"\n  {C_YELLOW}Applying patches...{C_RESET}")
                    patches.apply_hacks()
                    print(f"  {C_GREEN}Done. Press any key.{C_RESET}")
                    _wait_key()
            elif key in ("r", "routers"):
                _history_push("routers")
                render_routers(w)
                k2 = _read_key()
                if k2 in ("1", "2", "3"):
                    name = router.available()[int(k2) - 1]
                    router.set_active(name)
                    print(f"\n  {C_GREEN}Router set to {name}. Press any key.{C_RESET}")
                    _wait_key()
            elif key in ("b", "backup"):
                _history_push("backup")
                render_backup(w)
                k2 = _read_key()
                if k2 == "1":
                    print(f"\n  {C_YELLOW}Creating backup...{C_RESET}")
                    result = backup_create()
                    if result["ok"]:
                        print(f"  {C_GREEN}Backup: {result['path']} ({result['size_mb']}MB){C_RESET}")
                    else:
                        print(f"  {C_RED}Failed: {result}{C_RESET}")
                    _wait_key()
            elif key in ("w", "watch"):
                _history_push("watch")
                render_watch(w)
                _wait_key()
            elif key in ("c", "config"):
                _history_push("config")
                header("Config", "current atropos config")
                cfg = config.load()
                print(config.dump_yaml(cfg))
                print()
                print(f"  {C_BOLD}Press any key to continue{C_RESET}")
                _wait_key()
            elif key in ("e", "effort"):
                _history_push("effort")
                header("Effort", "per-harness tiers")
                for h in ("hermes", "claude", "atropos"):
                    print(f"  {C_CYAN}{h:<10}{C_RESET} {settings.get(f'effort.{h}', 'medium')}")
                print()
                print(f"  {C_BOLD}[1]{C_RESET} Set all → tryhard   {C_BOLD}[2]{C_RESET} Back")
                k2 = _read_key()
                if k2 == "1":
                    for h in ("hermes", "claude", "atropos"):
                        settings.set(f"effort.{h}", "tryhard")
                    print(f"  {C_GREEN}Effort → tryhard for all harnesses{C_RESET}")
                    _wait_key()
            elif key in ("m", "extensions"):
                _history_push("extensions")
                render_extensions(w)
                _wait_key()
            elif key in ("F", "fleet"):
                _history_push("fleet")
                render_fleet(w)
                k2 = _read_key()
                if k2 == "1":
                    print(f"\n  {C_YELLOW}Pinging fleet...{C_RESET}")
                    rows = fleet.ping("all")
                    for r in rows:
                        icon = "OK" if r.get("ok") else "FAIL"
                        print(f"    [{icon}] {r['name']}: {r.get('latency_ms', '—')}ms — {r.get('error') or r.get('version', '')}")
                    _wait_key()
            elif key in ("I", "identity"):
                _history_push("identity")
                render_identity(w)
                _wait_key()
            elif key in ("C", "configs"):
                _history_push("configs")
                render_configs(w)
                _wait_key()
            elif key in ("M", "memory"):
                _history_push("memory")
                render_memory(w)
                _wait_key()
            elif key in ("A", "audit"):
                _history_push("audit")
                render_audit(w)
                _wait_key()
            elif key in ("B", "budget"):
                _history_push("budget")
                render_budget(w)
                _wait_key()
            elif key in ("S", "snapshots"):
                _history_push("snapshots")
                render_snapshots(w)
                _wait_key()
            elif key in ("T", "activity"):
                _history_push("activity")
                render_activity(w)
                _wait_key()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"{SHOW_CURSOR}", end="")


def _read_key() -> str:
    """Read a single key from stdin (raw mode)."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":  # escape sequences
            # Check for arrow keys
            if os.read(fd, 1) == b"[":
                seq = os.read(fd, 1)
                if seq == b"A":
                    return "\x1b[A"  # up
                if seq == b"B":
                    return "\x1b[B"  # down
                if seq == b"C":
                    return "\x1b[C"
                if seq == b"D":
                    return "\x1b[D"
                return "\x1b"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _wait_key():
    _read_key()


if __name__ == "__main__":
    run()