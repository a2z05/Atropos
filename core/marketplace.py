#!/usr/bin/env python3
"""Atropos marketplace — trusted skill/plugin catalog, stdlib only.

Only a hardcoded allowlist of well-known sources is ever fetched; there is
no arbitrary-URL input anywhere. Install copies the fetched skill/plugin
into the correct local store (hermes skills, claude skills, hermes
plugins).

Sources (verified 2026-08-15):
  * anthropics/skills — official Anthropic skill collection (17 skills)
  * obra/superpowers — community skill framework (14 skills)
  * hermes-agent — NousResearch hermes-agent plugins with plugin.yaml (5)

Fetching is bounded (per-item size cap, timeout) and everything is
idempotent. On any source failure the catalog still lists the other
sources (per-item errors, never a whole-catalog failure).
"""
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor, as_completed

from . import detect, extensions
from .extensions import valid_name

GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_API = "https://api.github.com"

# Hosts the fetcher is ever allowed to talk to (SSRF-proof by construction).
ALLOWED_HOSTS = ("raw.githubusercontent.com", "api.github.com")

MAX_ITEM_BYTES = 5 * 1024 * 1024  # 5MB per fetched file
FETCH_TIMEOUT = 25

# Rust of extension names handled by the catalog — falls back to the
# directory listing when the API is unreachable.
NAME_ALIASES = {
    "anthropics/skills": {
        "algorithmic-art": "algorithmic-art",
        "brand-guidelines": "brand-guidelines",
        "canvas-design": "canvas-design",
        "claude-api": "claude-api",
        "doc-coauthoring": "doc-coauthoring",
        "docx": "docx",
        "frontend-design": "frontend-design",
        "internal-comms": "internal-comms",
        "mcp-builder": "mcp-builder",
        "pdf": "pdf",
        "pptx": "pptx",
        "skill-creator": "skill-creator",
        "slack-gif-creator": "slack-gif-creator",
        "theme-factory": "theme-factory",
        "web-artifacts-builder": "web-artifacts-builder",
        "webapp-testing": "webapp-testing",
        "xlsx": "xlsx",
    },
    "obra/superpowers": {
        "brainstorming": "brainstorming",
        "dispatching-parallel-agents": "dispatching-parallel-agents",
        "executing-plans": "executing-plans",
        "finishing-a-development-branch": "finishing-a-development-branch",
        "receiving-code-review": "receiving-code-review",
        "requesting-code-review": "requesting-code-review",
        "subagent-driven-development": "subagent-driven-development",
        "systematic-debugging": "systematic-debugging",
        "test-driven-development": "test-driven-development",
        "using-git-worktrees": "using-git-worktrees",
        "using-superpowers": "using-superpowers",
        "verification-before-completion": "verification-before-completion",
        "writing-plans": "writing-plans",
        "writing-skills": "writing-skills",
    },
}

# ── user-added sources (GitHub repos only, SSRF-proof) ────────────────────
# Persisted as JSON under the Atropos home: {sources: [{repo, subdir, branch,
# kind, target, name, author}]}. Repo URLs are parsed with str.startswith
# against the two allowed hosts and re-validated on every read.
_CUSTOM_SOURCES_FILE = "marketplace_custom.json"


def _custom_sources() -> list:
    """User-added sources, validated on every read (defense in depth)."""
    try:
        p = Path(detect.atropos_home()) / _CUSTOM_SOURCES_FILE
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        out = []
        for s in data.get("sources", []):
            if not isinstance(s, dict):
                continue
            repo = str(s.get("repo", ""))
            subdir = str(s.get("subdir", ""))
            branch = str(s.get("branch", "main"))
            if valid_repo(repo) and valid_subdir(subdir):
                out.append({
                    "id": f"{repo}/{subdir}".rstrip("/"),
                    "name": str(s.get("name") or repo.split("/")[-1]),
                    "author": str(s.get("author") or repo.split("/")[0]),
                    "kind": str(s.get("kind", "skill")),
                    "target": str(s.get("target", "hermes")),
                    "repo": repo,
                    "subdir": subdir,
                    "branch": branch,
                    "type": "dir-from-api",
                    "custom": True,
                    "description": str(s.get("description") or "Custom marketplace source"),
                })
        return out
    except Exception:
        return []


def _save_custom_sources(sources: list) -> dict:
    """Persist user-added sources; keep only valid entries."""
    try:
        p = Path(detect.atropos_home()) / _CUSTOM_SOURCES_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"sources": sources}, indent=2), encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def valid_repo(repo: str) -> bool:
    """Repo must be 'owner/name' on an allowed host (SSRF guard)."""
    if not repo or "/" not in repo:
        return False
    if not all(ch.isalnum() or ch in "._-" for ch in repo):
        return False
    return True


def valid_subdir(subdir: str) -> bool:
    """Subdir must be empty or a plain path segment (no '..', no leading /)."""
    if not subdir:
        return True
    if subdir.startswith("/") or ".." in subdir:
        return False
    return all(ch.isalnum() or ch in "._-/ " for ch in subdir)


def add_source(repo: str, subdir: str = "", branch: str = "main",
               kind: str = "skill", target: str = "hermes",
               name: str = "", author: str = "") -> dict:
    """Add a custom GitHub marketplace source (validated, persisted)."""
    repo = (repo or "").strip()
    subdir = (subdir or "").strip().strip("/")
    branch = (branch or "main").strip() or "main"
    kind = kind if kind in ("skill", "plugin") else "skill"
    target = target if target in ("hermes", "claude") else "hermes"
    if not valid_repo(repo):
        return {"ok": False, "error": "repo must be owner/name on github.com"}
    if not valid_subdir(subdir):
        return {"ok": False, "error": "invalid subdir"}
    sources = _custom_sources()
    src_id = f"{repo}/{subdir}".rstrip("/")
    if any(s["id"] == src_id for s in sources):
        return {"ok": False, "error": "source already added"}
    sources.append({
        "repo": repo, "subdir": subdir, "branch": branch,
        "kind": kind, "target": target,
        "name": (name or "").strip() or repo.split("/")[-1],
        "author": (author or "").strip() or repo.split("/")[0],
        "description": f"Custom GitHub source: {repo}/{subdir}".rstrip("/"),
    })
    return _save_custom_sources(sources)


def remove_source(source_id: str) -> dict:
    """Remove a user-added source by its id (repo/subdir)."""
    sources = _custom_sources()
    new = [s for s in sources if s["id"] != source_id]
    if len(new) == len(sources):
        return {"ok": False, "error": "source not found"}
    return _save_custom_sources(new)


# ── allowlist ─────────────────────────────────────────────────────────────
# Each source: {id, name, author, kind, target, type ('dir-from-api' |
# 'dir-from-alias'), repo, branch, subdir}
SOURCES = [
    {
        "id": "anthropics/skills",
        "name": "Anthropic Skills",
        "author": "Anthropic",
        "kind": "skill",
        "target": "claude",
        "repo": "anthropics/skills",
        "branch": "main",
        "subdir": "skills",
        "type": "dir-from-api",
        "description": "Official Anthropic skill collection (docx, pdf, xlsx, pptx, frontend-design, claude-api…).",
        "extra_files": ["LICENSE.txt"],
    },
    {
        "id": "obra/superpowers",
        "name": "Superpowers by obra",
        "author": "obra",
        "kind": "skill",
        "target": "claude",
        "repo": "obra/superpowers",
        "branch": "main",
        "subdir": "skills",
        "type": "dir-from-alias",
        "description": "Community skill framework — brainstorming, TDD, code review, systematic debugging.",
        "extra_files": [],
    },
    {
        "id": "hermes-agent/plugins",
        "name": "Hermes Plugin Registry",
        "author": "NousResearch",
        "kind": "plugin",
        "target": "hermes",
        "repo": "NousResearch/hermes-agent",
        "branch": "main",
        "subdir": "plugins",
        "type": "dir-from-api",
        "description": "Official hermes-agent plugins with plugin.yaml manifests (disk-cleanup, google_meet, spotify…).",
        "extra_files": ["README.md"],
    },
]


def _fetch(url: str, max_bytes: int = MAX_ITEM_BYTES) -> bytes:
    """Fetch a URL with host + size guards. Returns body bytes."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"fetch refused: host {host!r} not allowlisted")
    req = urllib.request.Request(url, headers={"User-Agent": "atropos-marketplace/1.2"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"item exceeds {max_bytes // 1024 // 1024}MB cap")
    return body


def _api_list(base: str, path: str, branch: str) -> list:
    """List a directory via the GitHub contents API.

    Falls back to subdir-level stubs when the API is unreachable (catalog
    must never fail whole — the brief's per-item error rule).
    """
    try:
        url = f"{GITHUB_API}/repos/{base}/contents/{path}?ref={branch}"
        raw = _fetch(url, 8 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        names = [d["name"] for d in data if d.get("type") == "dir"]
        return names
    except Exception:
        return None


def _desc_of(skill_name: str, source: dict) -> str:
    """Best-effort description from the item's frontmatter.

    Skills read SKILL.md; plugins read plugin.yaml (description field).
    """
    manifest = "plugin.yaml" if source["kind"] == "plugin" else "SKILL.md"
    key = "description" if source["kind"] == "plugin" else "description"
    url = (f"{GITHUB_RAW}/{source['repo']}/{source['branch']}/"
           f"{source['subdir']}/{skill_name}/{manifest}")
    try:
        body = _fetch(url, 64 * 1024).decode("utf-8", errors="replace")
    except Exception:
        return ""
    if source["kind"] == "plugin":
        for line in body.splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "description":
                return v.strip().strip('"')
        return ""
    if body.startswith("---"):
        end = body.find("---", 3)
        if end > 0:
            for line in body[3:end].splitlines():
                k, _, v = line.partition(":")
                if k.strip().lower() == key:
                    return v.strip().strip('"')
    return ""


def catalog() -> dict:
    """Full marketplace catalog with per-item install state.

    Returns {ok, sources: [{id, name, author, description, kind, target,
    items: [{id, name, installed, enabled}], error?}]}
    """
    installed = _installed_names()
    sources_out = []
    for src in SOURCES + _custom_sources():
        names = None
        if src["type"] == "dir-from-api":
            names = _api_list(src["repo"], src["subdir"], src["branch"])
            if names is None:
                # API unreachable: fall back to the alias table so the
                # catalog still renders (per-item error surfaced).
                names = list(NAME_ALIASES.get(src["id"], {}).keys())
                src = {**src, "_fallback": True}
        else:
            names = list(NAME_ALIASES.get(src["id"], {}).keys())
        if names is None:
            sources_out.append({**src, "error": "listing unavailable", "items": []})
            continue
        items = []
        valid_names = [n for n in names if valid_name(n)]
        # fetch descriptions concurrently (bounded) so the catalog never
        # serializes 30+ GitHub round-trips
        desc_map = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(_desc_of, n, src): n for n in valid_names}
            for fut in as_completed(futs, timeout=30):
                n = futs[fut]
                try:
                    desc_map[n] = fut.result()
                except Exception:
                    desc_map[n] = ""
        for n in valid_names:
            st = installed.get(("skill" if src["kind"] == "skill" else "plugin",
                                src["target"], n))
            items.append({
                "id": n,
                "name": n,
                "installed": bool(st),
                "enabled": bool(st and st.get("enabled")),
                "description": desc_map.get(n, ""),
            })
        sources_out.append({**src, "items": items})
    return {"ok": True, "sources": sources_out}


def _installed_names() -> dict:
    """{(kind, source, name): {'enabled': bool}} for installed items."""
    out = {}
    try:
        for e in extensions.list_extensions():
            out[(e["kind"], e["source"], e["name"])] = {"enabled": e["enabled"]}
    except Exception:
        pass
    return out


def _github_tree(base: str, branch: str, subdir: str) -> dict:
    """Recursive git-tree listing: {path: 'blob'|'tree'} under subdir."""
    try:
        url = f"{GITHUB_API}/repos/{base}/git/trees/{branch}?recursive=1"
        raw = _fetch(url, 10 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8", errors="replace"))
        out = {}
        for t in data.get("tree", []):
            p = t["path"]
            if p.startswith(subdir + "/"):
                rel = p[len(subdir) + 1:]
                if rel:
                    out[rel] = t["type"]
        return out
    except Exception:
        return {}


def install(source_id: str, item_name: str) -> dict:
    """Install one catalog item into the correct local store.

    Fetch = raw.githubusercontent.com paths derived only from the hardcoded
    allowlist source info + the validated item name. Returns result dict.
    """
    if not valid_name(item_name):
        return {"ok": False, "error": f"invalid item name: {item_name!r}"}
    src = next((s for s in SOURCES if s["id"] == source_id), None)
    if src is None:
        return {"ok": False, "error": f"unknown source: {source_id}"}
    if extensions.is_enabled(item_name, src["kind"], src["target"]) or _dir_present(item_name, src):
        return {"ok": False, "error": f"{item_name} already installed"}

    # Resolve the file list: try the recursive git tree first, else the
    # alias single-dir listing. _github_tree returns subdir-relative paths.
    tree = _github_tree(src["repo"], src["branch"], src["subdir"])
    files = {}
    if tree:
        prefix = f"{item_name}/"
        files = {p[len(prefix):]: t for p, t in tree.items() if p.startswith(prefix)}
    else:
        # fallback: try the items API dir listing
        try:
            url = (f"{GITHUB_API}/repos/{src['repo']}/contents/"
                   f"{src['subdir']}/{item_name}?ref={src['branch']}")
            raw = _fetch(url, 2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8", errors="replace"))
            files = {d["name"]: ("tree" if d.get("type") == "dir" else "blob")
                     for d in data} if isinstance(data, list) else {}
        except Exception:
            return {"ok": False, "error": "could not enumerate item files"}

    if not files:
        return {"ok": False, "error": f"no files found for {item_name}"}
    if "SKILL.md" not in files and src["kind"] == "skill":
        return {"ok": False, "error": f"{item_name} has no SKILL.md — not an installable skill"}

    root = _target_root(src["kind"], src["target"])
    dest = root / item_name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    fetched = 0
    errors = []
    for rel, typ in sorted(files.items()):
        if typ == "tree":
            continue
        # sanitize: only plain relative paths, no traversal
        if rel.startswith("/") or ".." in rel.split("/"):
            continue
        if not rel or "." in rel.split("/")[0]:  # no hidden top-level entries
            pass
        url = (f"{GITHUB_RAW}/{src['repo']}/{src['branch']}/"
               f"{src['subdir']}/{item_name}/{rel}")
        try:
            body = _fetch(url)
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            fetched += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    if fetched == 0:
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": f"nothing fetched for {item_name}"}

    # extra license/readme files
    for extra in src.get("extra_files", []):
        if extra in files:
            continue
        url = (f"{GITHUB_RAW}/{src['repo']}/{src['branch']}/"
               f"{src['subdir']}/{item_name}/{extra}")
        try:
            body = _fetch(url, 256 * 1024)
            (dest / extra).write_bytes(body)
        except Exception:
            pass

    return {
        "ok": True,
        "name": item_name,
        "kind": src["kind"],
        "source": src["target"],
        "path": str(dest),
        "files": fetched,
        "warnings": errors[:6],
    }


def _dir_present(item_name: str, src: dict) -> bool:
    """True when an item dir already exists in the target store."""
    try:
        root = _target_root(src["kind"], src["target"])
        return (root / item_name).exists()
    except Exception:
        return False


def _target_root(kind: str, target: str) -> Path:
    """Store root for a kind/target pair, matching extensions."""
    if kind == "skill":
        if target == "claude":
            return extensions.claude_skills_dir()
        return extensions.hermes_skills_dir()
    if target == "hermes":
        return extensions.hermes_plugins_dir()
    raise ValueError(f"unknown target: {target}")


def uninstall(item_name: str, kind: str, target: str) -> dict:
    """Remove an installed marketplace item (moves to trash)."""
    try:
        return extensions.remove(item_name, kind, target)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import json
    print(json.dumps(catalog(), ensure_ascii=False, indent=2)[:4000])