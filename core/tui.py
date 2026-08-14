#!/usr/bin/env python3
"""Atropos TUI — terminal user interface like Claude Code.

Runs `atropos tui` → interactive menu-driven terminal UI with:
- Colored boxes, status indicators, quick actions
- Keyboard navigation (arrows + enter, or 1-9 keys)
- Live status refresh (ESC to exit)

Pure stdlib (ANSI escape codes). Works on any terminal.
"""
import os
import shutil
import sys
import time
from datetime import datetime

from . import config, detect, doctor, patches, router
from .backup import create as backup_create, list_backups
from .watch import run_watch

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


def run() -> None:
    """Interactive TUI main loop."""
    if not _is_tty():
        print("tui requires a TTY. Try `atropos status` instead.")
        return

    print(f"{HIDE_CURSOR}", end="")
    try:
        w = _cols()
        while True:
            # main menu
            header("Command Center", "choose a panel")
            print()
            items = [
                ("[s]", "Status", "live system overview"),
                ("[d]", "Doctor", "health checks & fixes"),
                ("[p]", "Patches", "apply/verify hacks"),
                ("[r]", "Routers", "switch nain/omni/local"),
                ("[b]", "Backup", "create/list backups"),
                ("[w]", "Watch", "run self-healing checks"),
                ("[c]", "Config", "show config"),
                ("[q]", "Quit", "exit"),
            ]
            for idx, (key, name, desc) in enumerate(items):
                color = C_RED if name == "Quit" else C_CYAN
                print(f"  {C_BOLD}{color}{key}{C_RESET}  {C_BOLD}{name:<10}{C_RESET} {C_DIM}{desc}{C_RESET}")
            footer(w)

            # read key
            key = _read_key()
            if key in ("q", "\x1b"):
                print(f"{SHOW_CURSOR}")
                print(f"{C_GREEN}Bye{C_RESET}")
                break
            elif key in ("s", "1"):
                render_status_panel()
                _wait_key()
            elif key in ("d", "2"):
                render_doctor(w)
                k2 = _read_key()
                if k2 == "2":  # apply fixes
                    print(f"\n  {C_YELLOW}Applying fixes...{C_RESET}")
                    doctor.doctor(fix=True)
                    print(f"  {C_GREEN}Done. Press any key.{C_RESET}")
                    _wait_key()
            elif key in ("p", "3"):
                render_patches(w)
                k2 = _read_key()
                if k2 == "1":  # apply all
                    print(f"\n  {C_YELLOW}Applying patches...{C_RESET}")
                    patches.apply_hacks()
                    print(f"  {C_GREEN}Done. Press any key.{C_RESET}")
                    _wait_key()
            elif key in ("r", "4"):
                render_routers(w)
                k2 = _read_key()
                if k2 in ("1", "2", "3"):
                    name = router.available()[int(k2) - 1]
                    router.set_active(name)
                    print(f"\n  {C_GREEN}Router set to {name}. Press any key.{C_RESET}")
                    _wait_key()
            elif key in ("b", "5"):
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
            elif key in ("w", "6"):
                render_watch(w)
                _wait_key()
            elif key in ("c", "7"):
                header("Config", "current atropos config")
                cfg = config.load()
                print(config.dump_yaml(cfg))
                print()
                print(f"  {C_BOLD}Press any key to continue{C_RESET}")
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