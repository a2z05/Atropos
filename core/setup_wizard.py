#!/usr/bin/env python3
"""Atropos setup wizard — interactive installer + configurator.

Checks what's missing, offers to install, configures hermes + claude,
runs first-time doctor, and applies patches. TUI-driven.

Usage:
  atropos setup              # run wizard
  atropos setup --check      # check only (no install)
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, detect

# ── ANSI (same palette as tui.py) ─────────────────────────────────────────
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_CYAN = "\033[36m"
C_WHITE = "\033[37m"

CLEAR = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

# ── Helpers ────────────────────────────────────────────────────────────────

def _cols():
    try: return shutil.get_terminal_size().columns
    except: return 80


def _bar(text, w, color=C_CYAN):
    pad = max(1, w - len(text) - 4)
    return f"{color}┌─ {C_RESET}{text}{color} {'─' * pad}┐{C_RESET}"


def _line(w, color=C_CYAN):
    return f"{color}└{'─' * (w-2)}┘{C_RESET}"


def _center(text, w):
    if len(text) >= w: return text[:w-1]
    return " " * ((w - len(text)) // 2) + text


def _ok(msg):    print(f"  {C_GREEN}✓{C_RESET} {msg}")
def _fail(msg):  print(f"  {C_RED}✗{C_RESET} {msg}")
def _warn(msg):  print(f"  {C_YELLOW}⚠{C_RESET} {msg}")
def _info(msg):  print(f"  {C_CYAN}→{C_RESET} {msg}")


def _read_key():
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _run(cmd, timeout=60):
    """Run a shell command, return (exit_code, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


# ── Checks ─────────────────────────────────────────────────────────────────

class Check:
    """One install/check item."""
    def __init__(self, name, cmd, install_fn=None, description=""):
        self.name = name
        self.cmd = cmd
        self.install_fn = install_fn
        self.description = description
        self.installed = False
        self.version = None
        self.error = None

    def check(self):
        code, out, err = _run(self.cmd)
        self.installed = code == 0 and bool(out)
        self.version = out.split("\n")[0] if self.installed else None
        return self.installed

    def install(self):
        if self.install_fn:
            return self.install_fn()
        return False

    def status_line(self):
        icon = f"{C_GREEN}●{C_RESET}" if self.installed else f"{C_RED}●{C_RESET}"
        ver = self.version or "not found"
        return f"  {icon} {C_BOLD}{self.name:<20}{C_RESET} {C_DIM}{ver}{C_RESET}"


def _install_node():
    _info("Installing Node.js via nvm...")
    code, out, err = _run("curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash", timeout=120)
    if code != 0:
        _fail(f"nvm install failed: {err[:200]}")
        return False
    _run('export NVM_DIR="$HOME/.nvm" && [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" && nvm install --lts', timeout=300)
    return True


def _install_claude():
    _info("Installing Claude Code via npm...")
    code, out, err = _run("npm install -g @anthropic-ai/claude-code", timeout=300)
    if code != 0:
        _fail(f"npm install failed: {err[:200]}")
        return False
    return True


def _install_hermes():
    _info("Cloning Hermes Agent...")
    code, _, err = _run("git clone --depth 3 https://github.com/NousResearch/hermes-agent /opt/hermes-agent", timeout=120)
    if code != 0:
        _fail(f"git clone failed: {err[:200]}")
        return False
    _info("Installing Hermes dependencies...")
    _run("cd /opt/hermes-agent && pip install -r requirements.txt -q", timeout=300)
    return True


def _install_pip():
    _info("Installing pip (get-pip.py)...")
    code, _, err = _run("curl -sS https://bootstrap.pypa.io/get-pip.py | python3", timeout=120)
    return code == 0


def _install_gh():
    _info("Installing GitHub CLI...")
    code, _, err = _run("apt-get install -y gh 2>/dev/null || (curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && echo 'deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main' | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && apt-get update -qq && apt-get install -y gh)", timeout=300)
    return code == 0


def get_checks():
    """Build the list of checks based on detected environment."""
    checks = []

    # Python
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    checks.append(Check(
        "Python", f"{sys.executable} --version",
        None, "Required. Usually pre-installed."
    ))

    # pip
    checks.append(Check(
        "pip", f"{sys.executable} -m pip --version",
        _install_pip, "Python package manager"
    ))

    # Node.js
    checks.append(Check(
        "Node.js", "node --version",
        _install_node, "Required for Claude Code"
    ))

    # npm
    checks.append(Check(
        "npm", "npm --version",
        None, "Comes with Node.js"
    ))

    # Claude Code
    checks.append(Check(
        "Claude Code", "claude --version",
        _install_claude, "Anthropic coding agent"
    ))

    # Hermes Agent
    hermes_home = os.environ.get("HERMES_HOME", "/data/.hermes")
    hermes_bin = os.environ.get("HERMES_AGENT_PATH", "/opt/hermes-agent")
    checks.append(Check(
        "Hermes Agent", f"test -d {hermes_bin} && echo 'installed'",
        _install_hermes, "AI assistant gateway"
    ))

    # git
    checks.append(Check(
        "Git", "git --version",
        None, "Version control"
    ))

    # gh CLI
    checks.append(Check(
        "GitHub CLI", "gh --version",
        _install_gh, "For Atropos updates from GitHub"
    ))

    return checks


# ── First-time config ──────────────────────────────────────────────────────

def _configure_first_time():
    """Interactive config setup for first-time."""
    cfg = config.load()
    home = config.config_path().parent
    home.mkdir(parents=True, exist_ok=True)

    print(f"\n  {C_BOLD}{C_WHITE}First-time Configuration{C_RESET}\n")

    # Bot token
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if bot_token:
        _ok(f"Telegram bot token: ...{bot_token[-8:]}")
    else:
        _info("No TELEGRAM_BOT_TOKEN set. Add it to ~/.atropos/config.yaml later.")

    # Router
    router = cfg.get("router", {})
    if not router.get("active"):
        _info("Setting default router: nain")
        cfg.setdefault("router", {})["active"] = "nain"
        cfg["router"]["model"] = "deepmo"

    # Guest
    cfg.setdefault("guest", {})["enabled"] = False
    cfg["guest"]["persona_path"] = str(home / "guest_persona.md")

    config.save(cfg)
    _ok(f"Config saved: {config.config_path()}")


# ── Wizard UI ──────────────────────────────────────────────────────────────

def run_wizard(check_only=False):
    """Main setup wizard."""
    if not sys.stdout.isatty():
        print("setup requires a TTY. Try 'atropos doctor' instead.")
        return

    print(f"{HIDE_CURSOR}", end="")

    try:
        w = min(_cols(), 100)
        print(f"{CLEAR}")
        print(_center(f"{C_BOLD}{C_CYAN}⟁ ATROPOS SETUP{C_RESET}", w))
        print(_center(f"{C_DIM}interactive installer + configurator{C_RESET}", w))
        print()

        # Step 1: Detect
        print(f"  {C_BOLD}{C_WHITE}Step 1: Detect Environment{C_RESET}\n")
        r = detect.detect()
        for k, v in [("OS", f"{r.get('os','?')} {r.get('os_release','')}"),
                      ("Python", r.get("python_version","?")),
                      ("Cloud", r.get("cloud","none")),
                      ("Hermes", r.get("hermes_agent","not found")),
                      ("Claude", r.get("claude","not found"))]:
            print(f"    {C_CYAN}{k:<12}{C_RESET} {v}")
        print()

        # Step 2: Check installs
        print(f"  {C_BOLD}{C_WHITE}Step 2: Check Components{C_RESET}\n")
        checks = get_checks()
        for c in checks:
            c.check()
            print(c.status_line())

        missing = [c for c in checks if not c.installed and c.install_fn]

        if check_only:
            print(f"\n  {C_DIM}(--check mode, no installs){C_RESET}")
            if missing:
                print(f"\n  {C_YELLOW}Missing components that can be auto-installed:{C_RESET}")
                for c in missing:
                    print(f"    • {c.name}: {c.description}")
            footer(w, "Press any key to exit")
            _read_key()
            return

        # Step 3: Install missing
        if missing:
            print(f"\n  {C_BOLD}{C_WHITE}Step 3: Install Missing{C_RESET}\n")
            for i, c in enumerate(missing):
                print(f"    [{i+1}] {c.name}: {c.description}")
            print(f"    [0] Skip all\n")

            _info("Installing in 3 seconds... (press any key to choose)")
            # auto-install all
            for c in missing:
                print(f"\n  {C_YELLOW}Installing {c.name}...{C_RESET}")
                ok = c.install()
                if ok:
                    _ok(f"{c.name} installed")
                else:
                    _fail(f"{c.name} install failed")
        else:
            print(f"\n  {C_GREEN}All components installed!{C_RESET}")

        # Step 4: Configure
        print(f"\n  {C_BOLD}{C_WHITE}Step 4: Configure{C_RESET}\n")
        _configure_first_time()

        # Step 5: Doctor
        print(f"\n  {C_BOLD}{C_WHITE}Step 5: Doctor Check{C_RESET}\n")
        from .doctor import doctor as _doctor
        for r in _doctor(fix=True):
            icon = f"{C_GREEN}✓{C_RESET}" if r["ok"] else f"{C_RED}✗{C_RESET}"
            print(f"  {icon} {r['name']}: {r['msg']}")

        # Step 6: Apply patches
        print(f"\n  {C_BOLD}{C_WHITE}Step 6: Apply Patches{C_RESET}\n")
        from .patches import apply_hacks
        applied, skipped, errors = apply_hacks()
        _ok(f"Applied: {len(applied)}")
        for sid, reason in skipped:
            _warn(f"Skipped {sid}: {reason}")
        for e in errors:
            _fail(str(e))

        # Done
        print(f"\n  {C_BOLD}{C_WHITE}{'=' * (w-4)}{C_RESET}")
        print(_center(f"{C_BOLD}{C_GREEN}Atropos is ready!{C_RESET}", w))
        print(_center(f"Run {C_CYAN}atropos tui{C_RESET} for the interactive dashboard", w))
        print(_center(f"Run {C_CYAN}atropos doctor{C_RESET} for health check", w))
        print(_center(f"Run {C_CYAN}atropos dashboard{C_RESET} for web UI on :8787", w))
        print(f"  {C_BOLD}{C_WHITE}{'=' * (w-4)}{C_RESET}\n")
        footer(w, "Press any key to exit")
        _read_key()

    except KeyboardInterrupt:
        pass
    finally:
        print(f"{SHOW_CURSOR}", end="")


def footer(w, hint=""):
    print(f"{C_DIM}{'─' * w}{C_RESET}")
    if hint:
        print(f"{C_DIM}{hint}{C_RESET}")


if __name__ == "__main__":
    run_wizard()
