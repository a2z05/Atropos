#!/usr/bin/env python3
"""Atropos skills — universal skill routing and management.

Skills live in ~/.atropos/skills/ (one folder per skill, each with SKILL.md).
The harness router decides which agent (hermes / claude) handles which task
category. Both Hermes and Claude read from the same unified skill store.

Skill routing config lives in ~/.atropos/config.yaml under `skills:`.

v18 I machinery (adopted from Hermes agent/skill_utils.py + tools/
skill_manager_tool.py — cited per source):
  - ``skill_lint`` — frontmatter validation with the same rules as Hermes
    (required name/description, closing fence, body, 1024-char description
    budget, 100k content budget) but in pure stdlib (no PyYAML): the
    frontmatter parser is line-based and mirrors Hermes' simple fallback.
  - ``skill_matches_platform`` / ``skill_matches_environment`` — offer-time
    filters from Hermes (``platforms``/``environments`` frontmatter lists).
  - ``skill_view`` — load one skill's SKILL.md body (progressive disclosure,
    same as Hermes ``skill_view``).
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import config, detect, settings

# Hermes validation budgets (skill_manager_tool.py)
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
SKILL_PROMPT_DESC_LIMIT = 60

# Excluded dirs when scanning skills (agent/skill_utils.py)
EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))

PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}

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


# ── v18 I: lint / matching / view (adopted from Hermes) ───────────────────
def iter_skill_dirs(root: Path = None) -> list:
    """Yield skill dirs, excluding the Hermes metadata/support dirs.

    Both layouts are scanned: flat (~/.atropos/skills/<name>/) and Hermes
    nested (~/.hermes/skills/<category>/<name>/). A dir is a skill when it
    contains a SKILL.md (or a DESCRIPTION.md in the category-root sense —
    category dirs themselves are skipped, their children scanned).
    """
    base = root or skills_dir()
    out = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name in EXCLUDED_SKILL_DIRS:
            continue
        if (d / "SKILL.md").exists():
            out.append(d)
            continue
        # category dir — scan its children for skills
        for sub in sorted(d.iterdir()):
            if sub.is_dir() and sub.name not in EXCLUDED_SKILL_DIRS \
                    and (sub / "SKILL.md").exists():
                out.append(sub)
    return out


def parse_frontmatter(content: str):
    """(frontmatter, body) — stdlib port of Hermes skill_utils.parse_frontmatter.

    Strips a leading UTF-8 BOM (Windows editors), requires the ``---`` /
    ``---`` fence, parses ``key: value`` lines (no PyYAML — matches Hermes'
    own simple-key:value fallback path, which is what it uses when yaml is
    unavailable).
    """
    frontmatter = {}
    if content.startswith("﻿"):
        content = content[1:]
    body = content
    if not content.startswith("---"):
        return frontmatter, body
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body
    yaml_content = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]
    for line in yaml_content.strip().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        frontmatter[k.strip()] = v.strip().strip("'\"")
    return frontmatter, body


def skill_lint(name: str = None, root: Path = None) -> dict:
    """Lint skill SKILL.md frontmatter (Hermes rules, stdlib).

    ``root`` overrides the skills dir (for tests). Returns
    {total, ok, issues: [{skill, errors: [...]}]}.
    """
    base = iter_skill_dirs(root)
    if name:
        base = [d for d in base if d.name == name]
    issues = []
    for d in sorted(base):
        skill_file = d / "SKILL.md"
        if not skill_file.exists():
            issues.append({"skill": d.name, "errors": ["no SKILL.md"]})
            continue
        content = skill_file.read_text(encoding="utf-8", errors="ignore")
        errors = _lint_content(content, new_skill=False)
        if errors:
            issues.append({"skill": d.name, "errors": errors})
    return {"total": len(base), "ok": len(base) - len(issues), "issues": issues}


def _lint_content(content: str, new_skill: bool = False) -> list:
    """Validate SKILL.md — mirrors Hermes _validate_frontmatter."""
    if not content.strip():
        return ["content is empty"]
    content = content.lstrip("﻿")
    errors = []
    if not content.startswith("---"):
        errors.append("SKILL.md must start with YAML frontmatter (---)")
    meta, body = parse_frontmatter(content)
    if content.startswith("---") and not re.search(r"\n---\s*\n", content[3:]):
        errors.append("frontmatter not closed (no closing '---' line)")
    if "name" not in meta:
        errors.append("frontmatter must include 'name'")
    if "description" not in meta:
        errors.append("frontmatter must include 'description'")
    desc = str(meta.get("description", ""))
    if desc and len(desc) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"description exceeds {MAX_DESCRIPTION_LENGTH} chars")
    if new_skill and desc and len(desc.strip()) > SKILL_PROMPT_DESC_LIMIT:
        errors.append(
            f"new-skill description is {len(desc.strip())} chars — must fit the "
            f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget")
    if not body.strip():
        errors.append("SKILL.md must have content after the frontmatter")
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        errors.append(f"content is {len(content):,} chars (limit {MAX_SKILL_CONTENT_CHARS:,})")
    return errors


def skill_matches_platform(frontmatter: dict) -> bool:
    """True when the skill's ``platforms`` list matches this OS (Hermes rule).

    Absent/empty → compatible everywhere. Termux (sys.platform 'android' or
    'linux' + TERMUX env) accepts linux/termux/android tags.
    """
    platforms = frontmatter.get("platforms") or []
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    running_termux = os.environ.get("TERMUX_VERSION") is not None
    for p in platforms:
        mapped = PLATFORM_MAP.get(str(p).lower().strip(), str(p).lower().strip())
        if current.startswith(mapped):
            return True
        if running_termux and mapped in ("linux", "termux", "android"):
            return True
    return False


def skill_matches_environment(frontmatter: dict) -> bool:
    """True unless the skill declares ``environments`` that are not active.

    Offer-time filter only: an explicit view/load always wins. Atropos has no
    kanban/s6/docker env concept — ``env:``/``environments:`` entries other
    than the neutral ones are treated as inactive (the skill is hidden from
    the index until its environment is declared, e.g. via
    ``settings.skills.environments``).
    """
    environments = frontmatter.get("environments") or []
    if not environments:
        return True
    if not isinstance(environments, list):
        environments = [environments]
    active = set(settings.get("skills.environments", []) or [])
    return any(str(e) in active for e in environments)


def skill_view(name: str, root: Path = None) -> dict:
    """Load one skill: frontmatter + body + match flags (like Hermes skill_view).

    Looks in both flat and Hermes-nested layouts (category/name/).
    """
    base = root or skills_dir()
    candidates = [base / name]
    for d in iter_skill_dirs(base):
        if d.name == name and (d / "SKILL.md").exists():
            candidates.append(d)
    d = next((c for c in candidates if (c / "SKILL.md").exists()), None)
    if d is None:
        return {"ok": False, "error": f"skill {name!r} not found"}
    skill_file = d / "SKILL.md"
    content = skill_file.read_text(encoding="utf-8", errors="ignore")
    meta, body = parse_frontmatter(content)
    return {
        "ok": True,
        "name": name,
        "frontmatter": meta,
        "body": body,
        "platform_ok": skill_matches_platform(meta),
        "environment_ok": skill_matches_environment(meta),
        "matches": skill_matches_platform(meta) and skill_matches_environment(meta),
        "errors": _lint_content(content),
        "path": str(skill_file),
    }


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
