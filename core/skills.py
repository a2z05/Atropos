#!/usr/bin/env python3
"""Atropos skills — universal skill routing and management.

Skills live in ~/.atropos/skills/ (one folder per skill, each with SKILL.md).
The harness router decides which agent (hermes / claude) handles which task
category. Both Hermes and Claude read from the same unified skill store.

Skill routing config lives in ~/.atropos/config.yaml under `skills:`.
"""
import json
import os
import shutil
from pathlib import Path

from . import config, detect, settings

SKILL_CATEGORIES = [
    "coding",         # code generation, refactoring, debugging
    "devops",         # deploy, docker, CI/CD, infra
    "research",       # web search, paper lookup, analysis
    "creative",       # writing, design, content
    "productivity",   # docs, sheets, presentations
    "social-media",   # posting, monitoring
    "mlops",          # training, inference, model management
    "telegram",       # bot management, messaging
    "trading",        # crypto, finance
    "general",        # anything else
]

DEFAULT_ROUTING = {
    "coding": "claude",
    "devops": "claude",
    "debugging": "claude",
    "research": "hermes",
    "creative": "hermes",
    "productivity": "hermes",
    "social-media": "hermes",
    "mlops": "claude",
    "telegram": "hermes",
    "trading": "hermes",
    "general": "hermes",
}


def skills_dir() -> Path:
    d = detect.atropos_home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hermes_skills_dir() -> Path:
    """Hermes' own skills directory (symlink target)."""
    h = detect.hermes_home() / "skills"
    h.mkdir(parents=True, exist_ok=True)
    return h


def list_skills() -> list:
    """List all skills in the universal store."""
    sdir = skills_dir()
    result = []
    for d in sorted(sdir.iterdir()):
        if not d.is_dir():
            continue
        meta = _read_skill_meta(d)
        result.append({
            "name": d.name,
            "path": str(d),
            "category": meta.get("category", "general"),
            "description": meta.get("description", ""),
            "harness": get_routing(meta.get("category", "general")),
        })
    return result


def _read_skill_meta(skill_dir: Path) -> dict:
    """Read SKILL.md frontmatter for category/description."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return {}
    content = skill_file.read_text(encoding="utf-8", errors="ignore")[:2000]
    meta = {}
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
    return meta


def get_routing(category: str = "general") -> str:
    """Which harness handles this category? Returns 'hermes' or 'claude'."""
    routing = settings.get("skills.routing", DEFAULT_ROUTING) or DEFAULT_ROUTING
    return routing.get(category, "hermes")


def set_routing(category: str, harness: str):
    """Set which harness handles a category."""
    if harness not in ("hermes", "claude"):
        raise ValueError("harness must be 'hermes' or 'claude'")
    routing = dict(settings.get("skills.routing", DEFAULT_ROUTING) or DEFAULT_ROUTING)
    routing[category] = harness
    settings.set("skills.routing", routing)


def sync_to_hermes():
    """Copy universal skills into Hermes' skills directory.

    Symlinks require SeCreateSymbolicLinkPrivilege on Windows and break the
    sandboxed test homes — a plain copy (same as export_to_hermes) is always
    valid and keeps the two directions symmetric.
    """
    universal = skills_dir()
    hermes = hermes_skills_dir()
    synced = []
    for skill_dir in universal.iterdir():
        if not skill_dir.is_dir():
            continue
        target = hermes / skill_dir.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(skill_dir, target,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        synced.append(skill_dir.name)
    return synced
    universal = skills_dir()
    hermes = hermes_skills_dir()
    synced = []
    for skill_dir in universal.iterdir():
        if not skill_dir.is_dir():
            continue
        target = hermes / skill_dir.name
        if target.exists() or target.is_symlink():
            # update if different
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target, ignore_errors=True)
        os.symlink(str(skill_dir), str(target))
        synced.append(skill_dir.name)
    return synced


def import_from_hermes(skill_name: str = None):
    """Import skills from Hermes into the universal store."""
    universal = skills_dir()
    hermes = hermes_skills_dir()
    imported = []
    for d in (hermes / skill_name if skill_name else hermes).iterdir() if (hermes / skill_name if skill_name else hermes).exists() else []:
        if not d.is_dir():
            continue
        target = universal / d.name
        if not target.exists():
            shutil.copytree(d, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            imported.append(d.name)
    return imported


def export_to_hermes():
    """Copy universal skills into Hermes' skills dir (overwrite)."""
    universal = skills_dir()
    hermes = hermes_skills_dir()
    exported = []
    for skill_dir in universal.iterdir():
        if not skill_dir.is_dir():
            continue
        target = hermes / skill_dir.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(skill_dir, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        exported.append(skill_dir.name)
    return exported


if __name__ == "__main__":
    for s in list_skills():
        print(f"  {s['name']}: category={s['category']} → harness={s['harness']}")
