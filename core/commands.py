#!/usr/bin/env python3
"""Atropos universal commands & aliases, stdlib only.

The canonical store lives at ``~/.atropos/commands.json``::

    {
      "commands": [
        {"name": "docs", "template": "Write API docs for {topic}",
         "description": "Generate documentation", "mode": "claude"}
      ],
      "aliases": {"d": "docs"}
    }

A command's ``template`` is a *display string* (how the command expands
when the user types it) — never executable: nothing here ever runs a
template through a shell or subprocess. ``mode`` is a hint for which
harness handles the command (hermes / claude / atropos), defaulting to
``atropos``.

Names (commands and aliases) must match ``^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$``
— no dots, no slashes, no path traversal. Templates must be non-empty.

Pure stdlib. Never imports core.dashboard (circular).
"""
import json
import re
from pathlib import Path

from . import detect

# Safe identifier: alnum start, then alnum/underscore/dash, max 32 chars.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

MODES = ("hermes", "claude", "atropos")
DEFAULT_MODE = "atropos"

# Captured before the public ``list()`` function shadows the builtin, so
# code below can still build plain lists.
_BUILTIN_LIST = list


def valid_name(name: str) -> bool:
    """True when ``name`` is a safe command/alias identifier (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name))


def store_path() -> Path:
    """Canonical store file (~/.atropos/commands.json)."""
    return detect.atropos_home() / "commands.json"


def _empty_store() -> dict:
    """Fresh store shape: no commands, no aliases."""
    return {"commands": [], "aliases": {}}


def _load() -> dict:
    """Load the store; missing/corrupt files yield an empty store."""
    p = store_path()
    if not p.exists():
        return _empty_store()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    commands = data.get("commands")
    aliases = data.get("aliases")
    return {
        "commands": [c for c in commands if isinstance(c, dict)]
        if isinstance(commands, _BUILTIN_LIST) else [],
        "aliases": aliases if isinstance(aliases, dict) else {},
    }


def _save(store: dict):
    """Write the store, creating ~/.atropos on demand."""
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")


def _command(store: dict, name: str) -> dict | None:
    """Find a command entry by name."""
    for c in store["commands"]:
        if c.get("name") == name:
            return c
    return None


def _norm_mode(mode: str) -> str:
    """Coerce a mode hint into one of MODES (default DEFAULT_MODE)."""
    return mode if mode in MODES else DEFAULT_MODE


def add_command(name: str, template: str, description: str = "",
                mode: str = DEFAULT_MODE) -> dict:
    """Register a universal command.

    ``name`` must match the identifier regex, ``template`` must be
    non-empty (it is a display string — never executed). Duplicate names
    raise ValueError. Returns the new entry.
    """
    if not valid_name(name):
        raise ValueError(f"invalid command name: {name!r} "
                         f"(must match {NAME_RE.pattern})")
    template = (template or "").strip()
    if not template:
        raise ValueError("template must not be empty")
    store = _load()
    if _command(store, name) is not None:
        raise ValueError(f"command already exists: {name}")
    entry = {
        "name": name,
        "template": template,
        "description": (description or "").strip(),
        "mode": _norm_mode(mode),
    }
    store["commands"].append(entry)
    store["commands"].sort(key=lambda c: c["name"])
    _save(store)
    return entry


def remove_command(name: str) -> dict:
    """Remove a command and any aliases pointing at it.

    Raises FileNotFoundError when the command does not exist.
    """
    if not valid_name(name):
        raise ValueError(f"invalid command name: {name!r}")
    store = _load()
    entry = _command(store, name)
    if entry is None:
        raise FileNotFoundError(f"command not found: {name}")
    store["commands"] = [c for c in store["commands"] if c.get("name") != name]
    store["aliases"] = {a: t for a, t in store["aliases"].items() if t != name}
    _save(store)
    return {"ok": True, "name": name}


def add_alias(alias: str, target: str) -> dict:
    """Point an alias at an existing command (or another alias).

    Both names must match the identifier regex; the target must resolve
    to an existing command or alias. Re-aliasing an existing alias
    updates it. Returns {alias, target, resolves_to}.
    """
    if not valid_name(alias):
        raise ValueError(f"invalid alias name: {alias!r} "
                         f"(must match {NAME_RE.pattern})")
    if not valid_name(target):
        raise ValueError(f"invalid alias target: {target!r} "
                         f"(must match {NAME_RE.pattern})")
    if alias == target:
        raise ValueError("alias cannot point at itself")
    store = _load()
    resolved = resolve_alias(target, store=store)
    if resolved is None:
        raise ValueError(f"alias target not found: {target}")
    store["aliases"][alias] = target
    _save(store)
    return {"ok": True, "alias": alias, "target": target, "resolves_to": resolved}


def remove_alias(alias: str) -> dict:
    """Remove an alias. Raises FileNotFoundError when it does not exist."""
    if not valid_name(alias):
        raise ValueError(f"invalid alias name: {alias!r}")
    store = _load()
    if alias not in store["aliases"]:
        raise FileNotFoundError(f"alias not found: {alias}")
    del store["aliases"][alias]
    _save(store)
    return {"ok": True, "alias": alias}


def resolve_alias(name: str, store: dict | None = None) -> str | None:
    """Resolve ``name`` to a real command name.

    Commands resolve to themselves; aliases chase their target through
    alias chains (bounded) until a command is found or the chain loops.
    Returns None when the name is not a command or alias.
    """
    if store is None:
        store = _load()
    seen = set()
    current = name
    while current in store["aliases"]:
        if current in seen:
            return None
        seen.add(current)
        current = store["aliases"][current]
    if _command(store, current) is not None:
        return current
    return None


def list_commands() -> list:
    """Command entries, sorted by name."""
    return _BUILTIN_LIST(_load()["commands"])


def list_aliases() -> dict:
    """Alias map {alias: target}, sorted by alias."""
    aliases = _load()["aliases"]
    return dict(sorted(aliases.items()))


def list_all() -> dict:
    """Everything in the store: {commands: [...], aliases: {...}}."""
    return _load()


def stats() -> dict:
    """Store statistics: counts and mode breakdown."""
    store = _load()
    modes = {}
    for c in store["commands"]:
        m = c.get("mode", DEFAULT_MODE)
        modes[m] = modes.get(m, 0) + 1
    return {
        "commands": len(store["commands"]),
        "aliases": len(store["aliases"]),
        "modes": modes,
    }


def list() -> dict:
    """Full listing: {commands: [...], aliases: {...}} (alias of list_all)."""
    return _load()


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(list(), ensure_ascii=False, indent=2))
