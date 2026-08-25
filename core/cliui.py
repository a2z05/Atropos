#!/usr/bin/env python3
"""Atropos CLI UI — interactive menu, REPL, prompts, tables, progress.

Pure stdlib. Powers:
  * `atropos` with no args   → full-screen numbered menu (≤3 keys to anywhere)
  * `atropos repl`           → persistent REPL with history, tab-complete, `?`, slash-commands
  * `atropos <cmd> <...>`    → human tables (`--json` for machines)
  * everything else         → confirm()/select()/password() prompts
"""

import json
import os
import re
import shutil
import sys
import time
from datetime import date

from . import ascii as _ascii
from . import i18n, settings

# ── ANSI helpers ────────────────────────────────────────────────────────────
_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not (
    hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
)
def _c(code, s):
    return s if _NO_COLOR or not code else f"\033[{code}m{s}\033[0m"
def bold(s):  return _c("1", s)
def dim(s):   return _c("2", s)
def green(s): return _c("92", s)
def red(s):   return _c("91", s)
def yellow(s):return _c("93", s)
def cyan(s):  return _c("96", s)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
def visible(s): return _ANSI_RE.sub("", s)

def term_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


# ── lore: daily oracle line ────────────────────────────────────────────────
def oracle_line(lang: str | None = None) -> str:
    """One mythic line per day (seeded by date), from languages/*.json."""
    try:
        lines = _lang_list("lore_lines", lang)
        if not lines:
            return ""
        return lines[date.today().toordinal() % len(lines)]
    except Exception:
        return ""


def _lang_list(key: str, lang: str | None) -> list:
    val = i18n._load(lang or i18n.get_lang()).get(key)
    if isinstance(val, list) and val:
        return val
    en = i18n._load("en").get(key)
    return en if isinstance(en, list) else []


def tip(lang: str | None = None) -> str:
    t = _lang_list("tips", lang)
    return t[date.today().toordinal() % len(t)] if t else ""


def _lore_verdict(fail_count: int, lang: str | None = None) -> str:
    vs = _lang_list("lore_verdicts", lang)
    if not vs:
        vs = ["The thread is sound.", "A frayed strand.", "The weave is tearing."]
    if fail_count == 0:
        return vs[0]
    if fail_count <= 2:
        return vs[1]
    return vs[2]


# ── tables ─────────────────────────────────────────────────────────────────
def table(rows, headers=None, max_w=None, json_mode=False):
    """Render aligned rows; truncate with … to fit the terminal. JSON in json_mode."""
    if json_mode:
        return json.dumps(rows, indent=2, ensure_ascii=False)
    if max_w is None:
        max_w = term_width() - 2
    rows = [[str(c) for c in r] for r in rows]
    if headers:
        rows.insert(0, [str(h) for h in headers])
    ncol = max(len(r) for r in rows) if rows else 0
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [max(len(visible(rows[i][c])) for i in range(len(rows))) for c in range(ncol)]
    # shrink columns when the table is too wide (budget = prefix + separators)
    fixed = 2 + 3 * (ncol - 1)
    while sum(widths) + fixed > max_w:
        widest = max(range(ncol), key=lambda c: widths[c])
        if widths[widest] <= 1:
            break
        widths[widest] -= 1
    out = []
    for i, r in enumerate(rows):
        cells = []
        for c, w in enumerate(widths):
            cell = visible(r[c])
            if len(cell) > w:
                cell = cell[: max(w - 1, 1)] + "…" if w > 1 else "…"
            cells.append(cell.ljust(w))
        out.append("  " + "  ".join(cells).rstrip())
        if i == 0 and headers:
            out.append("  " + "─" * (sum(widths) + 3 * (ncol - 1)))
    return "\n".join(out)


# ── prompts ────────────────────────────────────────────────────────────────
def confirm(question: str, default: bool = False) -> bool:
    """y/N or Y/n prompt. Never a bare input: question + hint."""
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(f"{bold('?')} {question} [{hint}] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print(dim("  (answer y or n)"))


def select(question: str, options: list, default: int = 0) -> int:
    """Numbered menu; returns the chosen index. Accepts 1-9 or a name prefix."""
    print(f"{bold('?')} {question}")
    for i, opt in enumerate(options, 1):
        print(f"  {cyan(str(i))}  {opt}")
    while True:
        try:
            ans = input(dim(f"  choose [1-{len(options)}/name] ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans) - 1
        for i, opt in enumerate(options):
            if opt.lower().startswith(ans.lower()):
                return i
        print(dim("  not a choice — pick a number or a name"))


def password(prompt_text: str = "password") -> str:
    """Masked secret input (getpass). """
    import getpass
    try:
        return getpass.getpass(f"{bold('?')} {prompt_text}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def text(question: str, default: str = "", history: list | None = None) -> str:
    hint = f" [{default}]" if default else ""
    try:
        ans = input(f"{bold('?')} {question}{hint} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not ans and default:
        return default
    if ans and history is not None:
        history.insert(0, ans)
    return ans


# ── progress ───────────────────────────────────────────────────────────────
def progress(iterable, label: str = "", total: int | None = None, eta: bool = True):
    """Yield items with a live progress bar (no-op when not a TTY or tiny totals)."""
    total = total if total is not None else (len(iterable) if hasattr(iterable, "__len__") else 0)
    if total is None or total < 1 or _NO_COLOR or not getattr(sys.stdout, "isatty", lambda: False)():
        for item in iterable:
            yield item
        return
    width = max(term_width() - 30 - len(label), 10)
    start = time.monotonic()
    for i, item in enumerate(iterable, 1):
        yield item
        pct = i / total
        filled = int(width * pct)
        bar = "█" * filled + "░" * (width - filled)
        el = time.monotonic() - start
        eta_s = f"{int(el / i * (total - i))}s" if eta and i > 0 and pct > 0 else ""
        sys.stdout.write(f"\r  {label} {bar} {int(pct * 100)}% {eta_s}   ")
        sys.stdout.flush()
    sys.stdout.write("\r" + " " * (width + 40) + "\r")
    sys.stdout.flush()


# ── menu (bare atropos) ────────────────────────────────────────────────────
_MENU = [
    ("1", "Status", "system overview", "status"),
    ("2", "Doctor", "health checks + fix", "doctor"),
    ("3", "Backup", "create / list / restore", "backup --list"),
    ("4", "Sync", "push / pull / pair", "sync status"),
    ("5", "Update", "check / apply", "update --check"),
    ("6", "Skills", "list / enable / remove", "skills --list"),
    ("7", "MCP", "registry / rescan", "mcp list"),
    ("8", "Identity", "SOUL / AGENTS / edit", "identity list"),
    ("9", "Configs", "config manager", "configs list"),
    ("a", "Routing", "who does what", "routing list"),
    ("b", "Settings", "every key", "settings"),
    ("c", "Chat/REPL", "talk to the box", "repl"),
    ("d", "Dashboard", "web control plane", "dashboard"),
    ("q", "Quit", "", ""),
]


def menu(dispatch) -> int:
    """Full-screen numbered menu; dispatch maps a key to a CLI command."""
    while True:
        w = term_width()
        print("\033[2J\033[H", end="")
        # block glyphs need UTF-8 stdout (cp1252 consoles would raise)
        try:
            if sys.platform == "win32":
                sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(_ascii.banner() or "ATROPOS")
        print(dim(_ascii.t("CLOTHO · LACHESIS · ATROPOS") if hasattr(_ascii, "t") else "CLOTHO · LACHESIS · ATROPOS"))
        print(dim(oracle_line()) or "")
        print(dim(f"v{settings.get('version', '?')}  ·  {tip()}"))
        print()
        print(table([[k, f"{bold(label)}  {dim(desc)}", ""] for k, label, desc, _ in _MENU if k != "q"] + [("q", f"{bold('Quit')}  {dim('leave')}", "")], headers=None, max_w=w - 6))
        print()
        try:
            ans = input(dim("  choose (1-9, a-d, q) > ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not ans:
            continue
        if ans == "q":
            return 0
        for k, label, desc, cmd in _MENU:
            if k == ans:
                dispatch(cmd)
                input(dim("  press Enter to return to the menu…"))
                break


# ── REPL ───────────────────────────────────────────────────────────────────
_REPL_SLASH = {
    "/doctor": ("doctor", "run the 7 health checks"),
    "/backup": ("backup create", "create a backup now"),
    "/route": ("route", "show the active router"),
    "/effort": ("effort", "show effort tiers"),
    "/skills": ("skills --list", "list skills"),
    "/status": ("status", "system overview"),
    "/settings": ("settings", "settings table"),
    "/session": ("sessions current", "session engine: current session/thread"),
    "/thread": None,   # handled specially in repl()
    "/end": None,      # handled specially in repl()
    "/exit": None,
    "/quit": None,
    "/help": None,
}


def _repl_complete(text, state):
    if text.startswith("/"):
        cmds = [c for c in _REPL_SLASH if c.startswith(text)]
        return cmds[state] if state < len(cmds) else None
    return None


def repl(dispatch) -> int:
    """Persistent REPL: history, tab-complete, `?`, slash-commands, Ctrl+C safe."""
    hist_file = os.path.join(os.path.expanduser("~"), ".atropos", "history")
    history = []
    try:
        if os.path.exists(hist_file):
            with open(hist_file, encoding="utf-8", errors="replace") as f:
                history = [l.strip() for l in f.read().splitlines() if l.strip()][-200:]
    except Exception:
        pass
    try:
        import readline as _rl
        _rl.set_completer(_repl_complete)
        _rl.parse_and_bind("tab: complete")
        try:
            _rl.read_history_file(hist_file)
        except Exception:
            pass
    except ImportError:
        _rl = None
    print(_ascii.banner() or "ATROPOS")
    print(dim("CLOTHO · LACHESIS · ATROPOS — type `?` for help, `/exit` to quit"))
    # session-engine status in the prompt (best-effort, never crashes)
    def _prompt():
        try:
            from . import session_engine as _se
            sid = _se._current.get("cli", "")
            thr = _se._threads.get("cli", "")
            tag = (sid[:6] + ("·" + thr if thr else "")) if sid else ""
            return bold("atropos") + dim("[" + tag + "]> ") if tag else bold("atropos> ")
        except Exception:
            return bold("atropos> ")
    while True:
        try:
            line = input(_prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(dim("  session saved to ~/.atropos/history — see you soon."))
            return 0
        if not line:
            continue
        if line in ("?", "/help"):
            rows = [[f"{bold(c)}  {dim(v[1])}", ""] for c, v in _REPL_SLASH.items() if v]
            print(table(rows))
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/doctor":
            dispatch("doctor")
            continue
        if line == "/backup":
            dispatch("backup create")
            continue
        if line.startswith("/thread"):
            try:
                from . import session_engine as _se
                name = line[len("/thread"):].strip()
                r = _se.set_thread("cli", name)
                print(dim(f"  thread: {r.get('ok') and (name or 'general') or r.get('error', 'bad name')}"))
            except Exception as e:
                print(dim(f"  thread error: {e}"))
            continue
        if line == "/end":
            try:
                from . import session_engine as _se
                _se.set_thread("cli", "")
                print(dim("  thread ended — back to general"))
            except Exception:
                pass
            continue
        if line.startswith("/session"):
            rest = line[len("/session"):].strip()
            dispatch("sessions " + (rest or "current"))
            continue
        try:
            history.append(line)
            with open(hist_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        # marshall simple commands; void screen junk is fine
        if line.startswith("/"):
            cmds = _REPL_SLASH.get(line)
            if cmds:
                dispatch(cmds[0])
            else:
                print(dim(f"  unknown command: {line} — try /help"))
        else:
            dispatch(line)


# ── doctor verdict ─────────────────────────────────────────────────────────
def doctor_verdict(fail_count: int, lang: str | None = None) -> str:
    return _lore_verdict(fail_count, lang)


# ── session names ──────────────────────────────────────────────────────────
_NAMES_A = ["woven", "cut", "spun", "measured", "unturned", "bound", "threaded", "fated"]
_NAMES_B = ["at-dawn", "by-atropos", "of-clotho", "of-lachesis", "at-high-tide", "in-silk", "past-midnight", "in-ash"]


def session_name(seed: int) -> str:
    """Deterministic Moirai-flavored session name from an int seed."""
    a = _NAMES_A[seed % len(_NAMES_A)]
    b = _NAMES_B[(seed // len(_NAMES_A)) % len(_NAMES_B)]
    return f"thread-{a}-{b}"