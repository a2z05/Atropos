#!/usr/bin/env python3
"""Atropos ASCII identity — banner, threads, header. One source for CLI/TUI.

The banner is hand-drawn: a six-letter wordmark with a cut sliced through
the A by the Fates' shears, plus the tagline set in caps tracking. Glyphs
may use block characters; plain terminals fall back to #.

    b = banner()
    t = tui_header("1.4.1")
"""
import os
import shutil

# 4-row glyph set, every glyph 4 wide. `build_banner` composes ATROPOS
# with a cut gap between ATR and OPOS: the Fates' shears sit there on
# row 2, an empty slice elsewhere. All rows end up exactly 38 cells.
_GLYPHS = {
    "A": ["█▀▀█", "█▄▄█", "█  █", "▀  ▀"],
    "T": ["█▀▀█", "  █ ", "  █ ", "  ▀ "],
    "R": ["█▀▀▀", "█▀▀▄", "█  █", "▀  ▀"],
    "O": ["█▀▀█", "█▄▄█", "█  █", "▀  ▀"],
    "P": ["█▀▀▀", "█▀▀▄", "█   ", "▀   "],
    "S": ["█▀▀█", "█ ▀▄", "█▄▄▀", "▀  ▀"],
    " ": ["    ", "    ", "    ", "    "],
}
_CUT = ["   ", "   ", " ✂ ", "   "]


def _build_banner() -> list:
    rows = []
    for r in range(4):
        left = " ".join(_GLYPHS[c][r] for c in "ATR")
        right = " ".join(_GLYPHS[c][r] for c in "OPOS")
        rows.append(left + " " + _CUT[r] + " " + right)
    return rows

THREADS = [
    "   ║  ",
    "  ║ ║ ",
    " ║ ║ ║",
    "  ╚ ╝ ",
    "   ✂  ",
]

# theme accents for the TUI header frame
ACCENTS = {
    "black": "97",
    "dark": "36",
    "light": "90",
    "sepia": "33",
    "midnight": "36",
    "matrix": "92",
    "ink": "36",
    "embers": "31",
    "glass": "36",
}

# terminal-safe CLI theme list (dashboard gets the full set)
CLI_THEMES = ["dark", "light", "black"]


def _no_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return True
    term = os.environ.get("TERM", "")
    return "dumb" in term or not term


def _fmt(code, s):
    if not code or _no_color():
        return s
    return f"\033[{code}m{s}\033[0m"


def banner(color: bool = True) -> str:
    """The wordmark: monochrome glyphs (or ANSI on supported terminals)."""
    out = _build_banner()
    if color and not _no_color():
        out = [_fmt("96", l) for l in out]
    return "\n".join(out)


def threads() -> str:
    """The three-thread ornament — braided strands, a cut through them."""
    return "\n".join(THREADS)


def wordmark() -> str:
    """Plain ASCII fallback when block glyphs are unavailable."""
    return "\n".join(
        [
            "█▀▀█ █▀▀█ █▀▀▀   █▀▀█ █▀▀▀ █▀▀█ █▀▀█",
            "█▄▄█   █  █▀▀▄   █▄▄█ █▀▀▄ █▄▄█ █ ▀▄",
            "█  █   █  █  █ ✂ █  █ █    █  █ █▄▄▀",
            "▀  ▀   ▀  ▀  ▀   ▀  ▀ ▀    ▀  ▀ ▀  ▀",
        ]
    )


def _frame(lines, accent_code, w, title):
    pad = max(w - 2, 2)
    top = "╔" + "═" * pad + "╗"
    bot = "╚" + "═" * pad + "╝"
    out = [_fmt(accent_code, top)]
    for ln in lines:
        out.append(_fmt(accent_code, "║ ") + ln.ljust(pad - 1) + _fmt(accent_code, "║"))
    if title:
        t = f" {title} " if title else ""
        out.append(_fmt(accent_code, "║ ") + t.ljust(pad - 1) + _fmt(accent_code, "║"))
    out.append(_fmt(accent_code, bot))
    return "\n".join(out)


def tui_header(version: str, theme: str | None = None, width: int | None = None,
               color: bool = True) -> str:
    """Full-width TUI header: frame + wordmark + trio line + version."""
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    width = max(width, 60)
    accent = ACCENTS.get(theme or "dark", "36")
    lines = _build_banner() + [""]
    lines.append("CLOTHO · LACHESIS · ATROPOS")
    if version:
        lines.append(version)
    if not color:
        return _frame(lines, None, width, None)
    return _frame(lines, accent, width, None)


def _dim(s):
    if _no_color():
        return s
    return f"\033[2m{s}\033[0m"