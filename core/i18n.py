#!/usr/bin/env python3
"""Atropos i18n — flat key-value translations shared by CLI/TUI/chat.

Languages live in repo-root `languages/*.json` (en.json is the master).
Lookup: current language → English → raw key. Never an empty string.
"""
import json
import os
import sys

# languages/ sits next to the package (works from any cwd — realpath)
_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
_LANG_DIR = os.path.join(_REPO, "languages")

_cache: dict = {}
_default_lang = "en"


def available() -> list:
    """Sorted language codes found in languages/ (en first)."""
    langs = []
    try:
        for f in sorted(os.listdir(_LANG_DIR)):
            if f.endswith(".json") and f != "en.json":
                langs.append(f[:-5])
    except OSError:
        pass
    return ["en"] + langs


def _load(code: str) -> dict:
    if code not in _cache:
        try:
            with open(os.path.join(_LANG_DIR, f"{code}.json"), encoding="utf-8") as f:
                _cache[code] = json.load(f)
        except (OSError, ValueError):
            _cache[code] = {}
    return _cache[code]


def set_lang(code: str) -> None:
    """Switch the session language; unknown codes fall back to en."""
    global _default_lang
    _default_lang = code if code in available() else "en"


def get_lang() -> str:
    return _default_lang


def t(key: str, lang: str | None = None) -> str:
    """Translate a key: current lang → en → raw key."""
    code = lang or _default_lang
    if code != "en":
        val = _load(code).get(key)
        if isinstance(val, str):
            return val
    val = _load("en").get(key)
    return val if isinstance(val, str) else key


def keys(lang: str | None = None) -> list:
    """All keys the current (or given) language defines."""
    return sorted(_load(lang or _default_lang))


if __name__ == "__main__":
    if len(sys.argv) > 2:
        print(t(sys.argv[2], sys.argv[1]))
    elif len(sys.argv) > 1:
        print("\n".join(available()))
    else:
        print(", ".join(available()))