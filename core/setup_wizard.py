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
    if os.name == "nt":
        import msvcrt
        return msvcrt.getch().decode("utf-8", errors="replace")
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
    """Interactive config setup for first-time (reads Railway env vars too)."""
    cfg = config.load()
    home = config.config_path().parent
    home.mkdir(parents=True, exist_ok=True)

    print(f"\n  {C_BOLD}{C_WHITE}First-time Configuration{C_RESET}\n")

    # Bot token
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if bot_token:
        _ok(f"Telegram bot token: ...{bot_token[-8:]}")
        cfg.setdefault("telegram", {})["token"] = bot_token
    else:
        _info("No TELEGRAM_BOT_TOKEN set. Add it to ~/.atropos/config.yaml later.")

    # Owner IDs (comma-separated)
    owner_ids = os.environ.get("OWNER_IDS", "")
    if owner_ids:
        ids = [int(x.strip()) for x in owner_ids.split(",") if x.strip().isdigit()]
        if ids:
            cfg.setdefault("telegram", {})["owner_ids"] = ids
            _ok(f"Owner IDs: {ids}")

    # Router — read from env vars
    router = cfg.setdefault("router", {})
    if not router.get("active"):
        router["active"] = "nain"
        router["model"] = "deepmo"

    # 9Router / OmniRoute keys from env
    ninerouter_url = os.environ.get("NINEROUTER_URL", "")
    ninerouter_key = os.environ.get("NINEROUTER_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    if ninerouter_url:
        router["base_url"] = ninerouter_url
        _ok(f"9Router URL: {ninerouter_url[:40]}...")
    if ninerouter_key:
        _ok(f"9Router key: ...{ninerouter_key[-8:]}")
    if openai_key:
        _ok(f"OpenAI key: ...{openai_key[-8:]}")

    # Dashboard — bind to 0.0.0.0 on Railway
    cloud = "none"
    try:
        from . import detect as _detect
        cloud = _detect.detect_cloud()
    except Exception:
        pass
    if cloud == "railway":
        cfg.setdefault("dashboard", {})["host"] = "0.0.0.0"
        cfg["dashboard"]["port"] = int(os.environ.get("PORT", 8787))
        _ok("Railway detected: dashboard bound to 0.0.0.0")

    # Guest
    cfg.setdefault("guest", {})["enabled"] = False
    cfg["guest"]["persona_path"] = str(home / "guest_persona.md")

    config.save(cfg)
    _ok(f"Config saved: {config.config_path()}")


# ── Wizard UI ──────────────────────────────────────────────────────────────

# ── v1.4: discover / import / tour (shared by wizard + dashboard) ───────────

# resource groups: (group name, list of relative paths under the atropos home)
_GROUPS = {
    "mcp": ["mcp"],
    "models": ["models"],
    "webhooks": ["webhooks"],
    "routing": ["routing", "routing.yaml"],
    "identity": ["identity"],
    "memory": ["memory"],
    "links": ["links"],
    "commands": ["commands"],
    "files": ["files"],
}
_GROUP_NAMES = ("mcp", "models", "webhooks", "routing", "identity",
                "memory", "links", "commands", "files")


def _target_for(group: str) -> Path:
    """Atropos-side dir for a group (files/ go to ~/files-atropos)."""
    if group == "files":
        return Path(os.path.expanduser("~")) / "files-atropos"
    return detect.atropos_home() / group


def _harness_bins() -> dict:
    """{key: binary path or None} for claude + hermes."""
    return {
        "claude": detect._find_claude() or
                  (shutil.which("claude") or shutil.which("claude.exe") or None),
        "hermes": detect.hermes_agent() or None,
    }


def _claude_home_files() -> dict:
    """Claude-side placement: ~/files-claude exists -> that, else ~/Files dirs."""
    fh = Path(os.path.expanduser("~")) / "files-claude"
    if fh.is_dir():
        return {"dir": fh}
    return {"dir": None, "existing": [str(p) for p in
            Path(os.path.expanduser("~")).glob("Files*")][:5]}


def _hermes_files() -> list:
    """Hermes-side placement candidates that already exist."""
    return [str(d) for d in (Path(os.path.expanduser("~")) / "Files",
                             Path(os.path.expanduser("~")) / "files-hermes")
            if d.is_dir()]


def _monitor_recs(group: str) -> dict:
    """Per-resource monitors (probe-style guesses based on runtime files)."""
    out = {}
    if group == "mcp":
        for src in ("claude", "hermes"):
            outs = _run(f"ls {detect._home()}/.claude/mcp.json {detect.hermes_home()}/config.yaml 2>/dev/null")
            if outs[1]:
                out[src] = {"mode": "watch", "hint": f"{src} config file change"}
    if group == "models":
        out["status"] = {"mode": "probe",
                         "hint": "model env/ key drift across harnesses"}
    if group == "identity":
        out["status"] = {"mode": "probe", "hint": "persona identity files"}
    return out


def _group_names(group: str):
    """Candidate file names for a group + which harness owns them."""
    if group == "mcp":
        return ("mcp.json", "mcp/")   # hermes variant is content-based
    return (f"{group}.json", f"{group}.yaml", group)


def _hermes_has_mcp() -> bool:
    """Hermes mcp presence = config.yaml with an mcp/mcp_servers/plugins key."""
    p = detect.hermes_home() / "config.yaml"
    if not p.exists():
        return False
    try:
        data = config.parse_yaml(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and any(k in data for k in
                                          ("mcp", "mcp_servers", "plugins"))


def _has_group(group: str, harness: str) -> bool:
    """True when `harness` (claude|hermes) already carries `group`."""
    if group == "files":
        if harness == "claude":
            return (Path(os.path.expanduser("~")) / "files-claude").is_dir()
        return bool(list(Path(os.path.expanduser("~")).glob("Files*")))
    path = detect._home() / ".claude" if harness == "claude" \
        else detect.hermes_home()
    if group == "mcp" and harness == "hermes":
        return _hermes_has_mcp()
    return path.exists() and any((path / n).exists()
                                 for n in _group_names(group))


def _diff_group(group: str) -> list:
    """Per-target {target, marker_type, marker_val, mtime, size} for a group."""
    rows = []
    tg = _target_for(group)
    if tg.is_dir():
        for f in sorted(tg.rglob("*")):
            if f.name in ("secrets.json", "auth_token", "dashboard_auth.json") or \
               f.name.endswith((".env", ".pyc")) or "__pycache__" in f.parts:
                continue
            st = f.stat()
            rows.append({"target": str(f), "marker_type": "file", "marker_val": "",
                         "mtime": int(st.st_mtime), "size": st.st_size})
    return rows


def discover_summary() -> dict:
    """What the wizard can see: harnesses, groups, and messages."""
    bins = _harness_bins()
    out = {
        "ok": True,
        "harnesses": {
            "claude": {"present": bool(bins["claude"])},
            "hermes": {"present": bool(bins["hermes"])},
        },
        "groups": {},
        "total": {"claude": 0, "hermes": 0},
    }
    for s in _GROUP_NAMES:
        rec = {"name": s, "target_exists": _target_for(s).exists()}
        for h in ("claude", "hermes"):
            rec[h] = _has_group(s, h)
        rec["diff"] = _diff_group(s)
        out["groups"][s] = rec
    return out


def _import_group(group: str, harnesses: list, mode: str = "shared") -> dict:
    """Copy a resource group into atropos (mode: shared|per|monitor).

    Copies target files to the atropos side (notes only; later sync/pair steps
    handle live propagation). Returns a summary dict for CLI/dashboard.
    """
    tg = _target_for(group)
    if mode == "monitor":
        print(f"  [monitor] {group}: watching {', '.join(harnesses)} configs")
        return {"ok": True, "group": group, "mode": "monitor", "sources": harnesses}

    if group != "files":
        tg.mkdir(parents=True, exist_ok=True)
    copied = []
    for h in harnesses:
        src = (detect._home() / ".claude" if h == "claude" else detect.hermes_home())
        for n in ((f"{group}.json", f"{group}.yaml", group)
                  if group != "files" else ("Files*",)):
            p = None
            if n.endswith("*"):
                cands = sorted(Path(os.path.expanduser("~")).glob(n))
                p = cands[0] if cands else None
            else:
                p = src / n
            if p and p.exists():
                p2 = tg / (f"{h}-{p.name}" if mode == "per" else p.name)
                p2.parent.mkdir(parents=True, exist_ok=True)
                if p.is_dir():
                    shutil.copytree(p, p2, dirs_exist_ok=True)
                else:
                    shutil.copy2(p, p2)
                copied.append(str(p2))
    return {"ok": True, "group": group, "mode": mode, "sources": harnesses,
            "copied": copied}


def which_wins(group: str) -> str:
    """Which harness owns a group: 'claude' | 'hermes' | 'tie' | 'none'."""
    has = [h for h in ("claude", "hermes") if _has_group(group, h)]
    if not has:
        return "none"
    return has[0] if len(has) == 1 else "tie"


def _tour_seen() -> bool:
    t = detect.atropos_home() / "wizard_tour_seen"
    return t.exists()


def mark_tour_seen():
    t = detect.atropos_home() / "wizard_tour_seen"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("1", encoding="utf-8")


def tour(groups=("mcp", "models", "files"), steps=4) -> dict:
    """Dismissible onboarding tour. Argures: which groups + how many steps."""
    return {"ok": True, "steps": steps, "groups": list(groups),
            "seen": _tour_seen()}


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

        # Step 3: Install missing (ask per component)
        if missing:
            print(f"\n  {C_BOLD}{C_WHITE}Step 3: Install Missing{C_RESET}\n")
            for i, c in enumerate(missing):
                print(f"    [{i+1}] {c.name}: {c.description}")
            print(f"    [0] Skip all\n")
            for c in missing:
                try:
                    ans = input(f"  {C_CYAN}?{C_RESET} Install {C_BOLD}{c.name}{C_RESET}? [Y/n] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    ans = "n"
                if ans in ("n", "no", "0", "skip"):
                    _info(f"skipped {c.name}")
                    continue
                print(f"\n  {C_YELLOW}Installing {c.name}...{C_RESET}")
                ok = c.install()
                if ok:
                    _ok(f"{c.name} installed")
                else:
                    _fail(f"{c.name} install failed — you can install it later")
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
