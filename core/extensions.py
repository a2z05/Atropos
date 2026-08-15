#!/usr/bin/env python3
"""Atropos extensions — universal extension layer.

One abstraction over three stores:

  * Hermes skills    ``$HERMES_HOME/skills/<name>/SKILL.md``
  * Claude Code skills ``~/.claude/skills/<name>/SKILL.md``
  * Hermes plugins   ``$HERMES_HOME/plugins/<name>/plugin.yaml`` (+ code)

Provides enumerate / enable / disable / install / remove across all three
with a unified entry shape, safe name validation (identifier regex — no
path traversal), reversible disable (rename, never delete) and remove-to-
trash (``~/.atropos/trash``). Pure stdlib.
"""
import os
import re
import shutil
import time
from pathlib import Path

from . import detect, settings

# ── identifiers ───────────────────────────────────────────────────────────
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def valid_name(name: str) -> bool:
    """True when ``name`` is a safe extension identifier (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name))


KINDS = ("skill", "plugin")


def hermes_skills_dir() -> Path:
    """Hermes skills root (created on demand)."""
    d = detect.hermes_home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def claude_skills_dir() -> Path:
    """Claude Code skills root (created on demand)."""
    d = detect._home() / ".claude" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hermes_plugins_dir() -> Path:
    """Hermes plugins root (created on demand)."""
    d = detect.hermes_home() / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    return d


def trash_dir() -> Path:
    """Trash root for removed extensions (reversible)."""
    d = detect.atropos_home() / "trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _skill_enabled(skill_dir: Path) -> bool:
    """A skill is enabled when SKILL.md exists (disable renames it)."""
    return (skill_dir / "SKILL.md").exists()


def _plugin_enabled(plugin_dir: Path) -> bool:
    """A plugin is enabled when plugin.yaml exists and enabled != false."""
    yml = plugin_dir / "plugin.yaml"
    if not yml.exists():
        # plugins may be pure .py — treat presence as enabled
        return any(plugin_dir.glob("*.py"))
    try:
        from . import config
        data = config.parse_yaml(yml.read_text(encoding="utf-8"))
        return data.get("enabled", True) is not False
    except Exception:
        return True


def _read_meta(md: Path) -> dict:
    """Read SKILL.md frontmatter (name/description/category/harness)."""
    if not md.exists():
        return {}
    meta = {}
    try:
        head = md.read_text(encoding="utf-8", errors="replace")[:2000]
    except Exception:
        return {}
    if head.startswith("---"):
        end = head.find("---", 3)
        if end > 0:
            for line in head[3:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
    return meta


# ── enumerate ─────────────────────────────────────────────────────────────
def _iter_skill_dirs(where: str):
    """Yield (source, dir) pairs for one skill store."""
    root = claude_skills_dir() if where == "claude" else hermes_skills_dir()
    for d in sorted(root.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            yield where, d


def _iter_plugin_dirs():
    """Yield hermes plugin dirs (plugin.yaml or .py)."""
    root = hermes_plugins_dir()
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        yml = d / "plugin.yaml"
        if yml.exists() or any(d.glob("*.py")):
            yield "hermes", d


def list_extensions(kind: str = "all") -> list:
    """Unified listing across every store.

    ``kind`` is ``all`` | ``skill`` | ``plugin``. Each entry:
    {name, kind, source ('hermes'|'claude'), path, enabled, description,
     head, version}.
    """
    if not settings.get("extensions.enabled", True):
        return []
    out = []
    if kind in ("all", "skill"):
        for where, d in list(_iter_skill_dirs("hermes")) + list(_iter_skill_dirs("claude")):
            md = d / "SKILL.md"
            meta = _read_meta(md)
            out.append({
                "name": d.name,
                "kind": "skill",
                "source": where,
                "path": str(md),
                "enabled": _skill_enabled(d),
                "description": meta.get("description", ""),
                "category": meta.get("category", ""),
                "harness": meta.get("harness", ""),
                "head": md.read_text(encoding="utf-8", errors="replace")[:240] if md.exists() else "",
                "version": meta.get("version", ""),
            })
    if kind in ("all", "plugin"):
        for where, d in _iter_plugin_dirs():
            yml = d / "plugin.yaml"
            meta = {}
            if yml.exists():
                try:
                    from . import config
                    meta = config.parse_yaml(yml.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            out.append({
                "name": d.name,
                "kind": "plugin",
                "source": where,
                "path": str(d),
                "enabled": _plugin_enabled(d),
                "description": meta.get("description", ""),
                "category": meta.get("kind", ""),
                "harness": "hermes",
                "head": str(d),
                "version": str(meta.get("version", "")),
            })
    return out


def _resolve_root(kind: str, source: str) -> Path:
    """Root directory for a kind/source pair."""
    if kind == "skill":
        return claude_skills_dir() if source == "claude" else hermes_skills_dir()
    if kind == "plugin":
        if source not in ("hermes",):
            raise ValueError(f"plugins must be installed into hermes, got {source!r}")
        return hermes_plugins_dir()
    raise ValueError(f"unknown kind: {kind}")


# ── enable/disable ────────────────────────────────────────────────────────
def disable(name: str, kind: str = "skill", source: str = "hermes") -> dict:
    """Disable an extension reversibly (rename, never destructive).

    Skills: SKILL.md → SKILL.md.disabled. Plugins: plugin.yaml gains
    ``enabled: false`` (line edit that preserves comments).
    """
    if not valid_name(name):
        raise ValueError(f"invalid extension name: {name!r}")
    root = _resolve_root(kind, source)
    target = root / name
    if not target.exists():
        raise FileNotFoundError(f"extension not found: {name}")
    if kind == "skill":
        md = target / "SKILL.md"
        if md.exists():
            md.rename(target / "SKILL.md.disabled")
    else:
        yml = target / "plugin.yaml"
        if yml.exists():
            text = yml.read_text(encoding="utf-8")
            if "enabled:" in text:
                import re as _re
                text = _re.sub(r"^enabled:.*$", "enabled: false", text, count=1, flags=_re.MULTILINE)
            else:
                text = text.rstrip() + "\nenabled: false\n"
            yml.write_text(text, encoding="utf-8")
    return {"ok": True, "name": name, "kind": kind, "source": source, "enabled": False}


def enable(name: str, kind: str = "skill", source: str = "hermes") -> dict:
    """Re-enable a disabled extension (inverse of disable)."""
    if not valid_name(name):
        raise ValueError(f"invalid extension name: {name!r}")
    root = _resolve_root(kind, source)
    target = root / name
    if not target.exists():
        raise FileNotFoundError(f"extension not found: {name}")
    if kind == "skill":
        md = target / "SKILL.md.disabled"
        if md.exists():
            md.rename(target / "SKILL.md")
    else:
        yml = target / "plugin.yaml"
        if yml.exists():
            text = yml.read_text(encoding="utf-8")
            if "enabled:" in text:
                import re as _re
                text = _re.sub(r"^enabled:.*$", "enabled: true", text, count=1, flags=_re.MULTILINE)
            else:
                text = text.rstrip() + "\nenabled: true\n"
            yml.write_text(text, encoding="utf-8")
    return {"ok": True, "name": name, "kind": kind, "source": source, "enabled": True}


def is_enabled(name: str, kind: str = "skill", source: str = "hermes") -> bool:
    """Enabled state of one extension."""
    root = _resolve_root(kind, source)
    target = root / name
    if not target.exists():
        return False
    if kind == "skill":
        return _skill_enabled(target)
    return _plugin_enabled(target)


# ── install / remove ──────────────────────────────────────────────────────
def install(src: Path, name: str, kind: str = "skill", source: str = "hermes") -> dict:
    """Copy an extension directory into the correct store root.

    ``src`` is a local directory (from the universal store or a
    marketplace checkout). Destructive-copy semantics match
    skills.export_to_hermes: existing target is replaced.
    """
    if not valid_name(name):
        raise ValueError(f"invalid extension name: {name!r}")
    if not src.is_dir():
        raise FileNotFoundError(f"source dir not found: {src}")
    root = _resolve_root(kind, source)
    dest = root / name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return {"ok": True, "name": name, "kind": kind, "source": source, "path": str(dest)}


def remove(name: str, kind: str = "skill", source: str = "hermes", trash: bool = True) -> dict:
    """Remove an extension — moved to ~/.atropos/trash (reversible).

    Pass ``trash=False`` for a permanent delete (used by trash-cleanup).
    """
    if not valid_name(name):
        raise ValueError(f"invalid extension name: {name!r}")
    root = _resolve_root(kind, source)
    target = root / name
    if not target.exists():
        raise FileNotFoundError(f"extension not found: {name}")
    if trash:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = trash_dir() / f"{name}_{stamp}"
        shutil.move(str(target), str(dest))
        return {"ok": True, "name": name, "kind": kind, "trashed": str(dest)}
    shutil.rmtree(target, ignore_errors=True)
    return {"ok": True, "name": name, "kind": kind, "removed": True}


def list_trash() -> list:
    """Entries currently sitting in the trash."""
    root = trash_dir()
    if not root.exists():
        return []
    return [{"name": p.name, "path": str(p)} for p in sorted(root.iterdir()) if p.is_dir()]


def restore_from_trash(trash_name: str, kind: str = "skill", source: str = "hermes") -> dict:
    """Move an item back out of trash into its store."""
    src = trash_dir() / trash_name
    if not src.is_dir():
        raise FileNotFoundError(f"trash item not found: {trash_name}")
    # original name is the prefix before the timestamp suffix
    base = trash_name.rsplit("_", 2)[0]
    if not valid_name(base):
        raise ValueError(f"unresolvable trash name: {trash_name!r}")
    root = _resolve_root(kind, source)
    dest = root / base
    shutil.move(str(src), str(dest))
    return {"ok": True, "name": base, "path": str(dest)}


def empty_trash() -> int:
    """Permanently delete all trash entries. Returns count removed."""
    root = trash_dir()
    count = 0
    for p in root.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    return count


if __name__ == "__main__":
    for e in list_extensions():
        state = "on" if e["enabled"] else "off"
        print(f"  [{state}] {e['kind']:<6} {e['source']:<7} {e['name']}")