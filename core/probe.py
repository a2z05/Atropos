#!/usr/bin/env python3
"""Atropos capability probe — what each harness currently exposes.

``probe_capabilities()`` returns feature names per harness:

    {
      "hermes":   [panels, skills, plugins, version...],
      "claude":   [slash-commands, permission-modes, mcp-servers...],
      "omni":     [routers, models...],
      "ninerouter": [models, kinds...],
      "atropos":  [fixed builtin features],
    }

The dashboard uses this for its section registry: sections whose
`requires` capability is missing are hidden; unknown capabilities get a
generic preview card instead of a blank tile.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from . import detect, router
from . import skills as skills_mod


def probe_hermes() -> list:
    feats = []
    home = detect.hermes_home()
    if home and home.is_dir():
        feats.append("present")
        (home / "config.yaml").exists() and feats.append("config")
        (home / ".env").exists() and feats.append("env")
        ws = home / "workspace"
        if ws.is_dir():
            feats.append("workspace")
            any(ws.glob("**/*.md")) and feats.append("docs")
        # python-telegram-bot presence via plugin dirs
        plugins = home / "plugins"
        if plugins.is_dir() and any(plugins.iterdir()):
            feats.append("plugins")
            for p in sorted(plugins.iterdir()):
                if p.is_dir() and (p / "adapter.py").exists():
                    feats.append(f"plugin:{p.name}")
        # skills it exposes (dir or manifest)
        for sf in ("skills", "assets/skills"):
            d = home / sf
            if d.is_dir() and any(d.iterdir()):
                feats.append(f"skills:{sf}")
    version = _hermes_version()
    if version:
        feats.append(f"version:{version}")
    return sorted(set(feats))


def _hermes_version() -> str | None:
    repo = detect.hermes_agent()
    if not repo:
        return None
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def probe_claude() -> list:
    feats = []
    if detect._find_claude():
        feats.append("present")
    claude_dir = detect._home() / ".claude"
    if claude_dir.is_dir():
        feats.append("config-dir")
        (claude_dir / "settings.json").exists() and feats.append("settings")
        (claude_dir / "mcp.json").exists() and feats.append("mcp")
        hooks = claude_dir / "hooks"
        if hooks.is_dir() and any(hooks.iterdir()):
            feats.append("hooks")
        skills = claude_dir / "skills"
        if skills.is_dir() and any(skills.iterdir()):
            feats.append("skills")
    # our permission presets are projected for claude
    try:
        from . import settings as st
        preset = st.get("permissions.preset", "default")
        feats.append(f"permissions:{preset}")
    except Exception:
        pass
    return sorted(set(feats))


def probe_omni() -> list:
    feats = []
    url = os.environ.get("OMNIROUTE_BASE_URL", "")
    if url:
        feats.append("base-url")
    try:
        proc = subprocess.run(["omniroute", "--help"], capture_output=True, text=True, timeout=15)
        if proc.returncode == 0:
            feats.append("cli")
    except Exception:
        pass
    try:
        from . import router as _r
        models = _r.discover_models("omni")
        if models.get("ok"):
            feats.append("models")
    except Exception:
        pass
    return sorted(set(feats))


def probe_ninerouter() -> list:
    feats = []
    url = os.environ.get("NINEROUTER_URL", "")
    if url:
        feats.append("base-url")
    try:
        from . import router as _r
        m = _r.discover_models("nain")
        if m.get("ok"):
            feats.append("models")
            feats.extend(f"model:{mid}" for mid in m.get("models", [])[:5])
    except Exception:
        pass
    return sorted(set(feats))


def probe_atropos() -> list:
    """Fixed builtin capabilities of the Atropos layer itself."""
    out = ["dashboard", "updates", "self-heal", "backups", "sync",
           "identity", "configs", "mcp", "routing", "agents", "middleware",
           "guest-isolation", "telegram-out"]
    try:
        if skills_mod.list_skills():
            out.append("skills")
    except Exception:
        pass
    return sorted(out)


def probe_capabilities() -> dict:
    return {
        "hermes": probe_hermes(),
        "claude": probe_claude(),
        "omni": probe_omni(),
        "ninerouter": probe_ninerouter(),
        "atropos": probe_atropos(),
    }


# dashboard section registry — which capability gates each section
SECTION_REQUIRES = {
    "sessions": "hermes:present",
    "cron": "hermes:config",
    "plugins": "hermes:plugins",
    "mcp": "claude:mcp",
    "models": "omni:models",
    "routing": "atropos:routing",
    "agents": "atropos:agents",
    "middleware": None,  # always available
}


def available_sections(registry: dict, caps: dict | None = None) -> list:
    """Sections whose `requires` capability is present (None = always)."""
    caps = caps or probe_capabilities()
    flat = set()
    for h, feats in caps.items():
        flat.update(f"{h}:{f}" for f in feats)
        flat.update(feats)
    out = []
    for sid, req in registry.items():
        if req is None or req in flat:
            out.append(sid)
    return out


if __name__ == "__main__":
    print(json.dumps(probe_capabilities(), indent=2, ensure_ascii=False))