#!/usr/bin/env python3
"""Auto-improve engine (v18 F) — self-learning system.

Pieces:
  - ``UsageTracker`` — per-skill usage telemetry (~/.atropos/skills/.usage.json):
    views, runs, last-used; lifecycle active -> stale (30d unused) ->
    archived (90d, moved to .archive/); pinned skills exempt.
  - ``auto_skill_offer(transcript)`` — after a complex task (5+ tool calls,
    user confirms), offer to save a skill. settings.auto_skill gates it.
  - ``auto_memory_offer()`` — after a session, [Save to memory] prompt hook
    (settings.auto_memory gates).
  - ``AttributionLog`` — every file edit records who/what did it
    (~/.atropos/attribution.jsonl); attribution(file) shows history.
  - ``Curator`` — weekly consolidate: scan skills, merge duplicates, prune
    stale, report; atropos curator status|run.
  - ``orchestrate(goal, agents, deps)`` — multi-agent subtask execution with
    dependency ordering and a merge step (thin wrapper over core/agents.

Adapted from Hermes skill_provenance.py / skill_usage.py (provenance +
usage telemetry) — cited per Hermes source.
"""
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import detect, settings

_SKILL_USAGE = "skills/.usage.json"
_ATTRIBUTION = "attribution.jsonl"
_MIGRATIONS = "migrations.jsonl"  # shared log format with migrate.py


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path, default=None):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── usage telemetry (skill_usage.py) ───────────────────────────────────────
def _usage_path() -> Path:
    return detect.atropos_home() / _SKILL_USAGE


def usage_path() -> Path:
    """Exposed for the dashboard Skills panel."""
    return _usage_path()


def record_usage(skill: str, kind: str = "run"):
    """Record one view/run of a skill. Returns the new entry."""
    usage = _load_json(_usage_path(), {})
    entry = usage.get(skill, {})
    entry[kind] = entry.get(kind, 0) + 1
    entry["last_used"] = _now()
    usage[skill] = entry
    _save_json(_usage_path(), usage)
    return entry


def usage_stats() -> dict:
    """{skill: {views, runs, last_used, lifecycle}} for the panel."""
    usage = _load_json(_usage_path(), {})
    out = {}
    for name, d in usage.items():
        d["lifecycle"] = lifecycle(name)
        out[name] = d
    return out


def lifecycle(name: str, now=None) -> str:
    """active -> stale (30d unused) -> archived (90d); pinned exempt."""
    from . import skills as _skills
    meta = _skills._read_skill_meta(skills_dir_for(name))
    pinned = "pinned" in (meta.get("tags") or []) or meta.get("pinned")
    if pinned:
        return "pinned"
    usage = _load_json(_usage_path(), {})
    last = usage.get(name, {}).get("last_used")
    if not last:
        return "active"  # never run — treat as active (new)
    try:
        dt = datetime.fromisoformat(last)
    except ValueError:
        return "active"
    now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    days = (now_dt - dt).days
    if days > 90:
        return "archived"
    if days > 30:
        return "stale"
    return "active"


def skills_dir_for(name: str) -> Path:
    return detect.atropos_home() / "skills" / name


def _archive_dir() -> Path:
    return detect.atropos_home() / "skills" / ".archive"


def sweep_lifecycle(now=None) -> list:
    """Move stale->archived when past 90d; returns transitions [{name, from, to}]."""
    usage = _load_json(_usage_path(), {})
    transitions = []
    for name in usage:
        state = lifecycle(name, now)
        src = skills_dir_for(name)
        if state == "archived" and src.exists():
            _archive_dir().mkdir(parents=True, exist_ok=True)
            dst = _archive_dir() / name
            if not dst.exists():
                shutil.move(str(src), str(dst))
                transitions.append({"name": name, "from": "active/stale", "to": "archived"})
    return transitions


# ── auto-skill creation (Hermes skill_provenance.py) ───────────────────────
def auto_skill_offer(transcript: list, confirmed: bool = False, threshold: int = 5) -> dict:
    """Offer to save a skill after a complex task (5+ tool calls).

    settings.skills.auto_skill (on|off) gates the offer. Returns {ok,
    offered, skill_name?, reason} — never writes unless confirmed.
    """
    if not settings.get("skills.auto_skill", False):
        return {"ok": False, "offered": False, "reason": "auto_skill off"}
    if len(transcript) < threshold:
        return {"ok": False, "offered": False,
                "reason": f"only {len(transcript)} steps (need {threshold}+)"}
    name = _skill_name_from_transcript(transcript)
    if not confirmed:
        return {"ok": True, "offered": True, "skill_name": name,
                "reason": "awaiting confirmation"}
    return save_auto_skill(name, transcript)


def _skill_name_from_transcript(transcript: list) -> str:
    """Derive a kebab-case skill name from the transcript's first task line."""
    text = " ".join(str(t) for t in transcript[:2])
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9-]*", text)
    keep = [w for w in words if w.lower() not in ("the", "a", "an", "and", "to")]
    base = "-".join(keep[:4]) or "auto-skill"
    return base.lower()


def save_auto_skill(name: str, transcript: list) -> dict:
    """Create a skill dir with a frontmatter'd SKILL.md from the transcript."""
    from . import skills as _skills
    d = skills_dir_for(name)
    d.mkdir(parents=True, exist_ok=True)
    md = (d / "SKILL.md")
    if md.exists():
        return {"ok": False, "error": f"skill {name} already exists"}
    body = ("---\n"
            f"name: {name}\n"
            "description: Auto-created from a completed task\n"
            "category: auto\n"
            "provenance: auto\n"
            "---\n\n"
            + "\n".join(f"- {line}" for line in transcript[:10])
            + "\n")
    md.write_text(body, encoding="utf-8")
    _skills.sync_to_hermes()
    record_usage(name, "view")
    return {"ok": True, "skill_name": name, "path": str(md)}


def auto_memory_offer(context: str, confirmed: bool = False) -> dict:
    """After a session, [Save to memory] hook. settings.skills.auto_memory gates."""
    if not settings.get("skills.auto_memory", False):
        return {"ok": False, "offered": False, "reason": "auto_memory off"}
    if not context or len(context.strip()) < 20:
        return {"ok": False, "offered": False, "reason": "nothing durable"}
    if not confirmed:
        return {"ok": True, "offered": True, "reason": "awaiting confirmation"}
    from . import memory
    memory.add("auto-session-note", context[:500])
    return {"ok": True, "offered": True, "saved": "memory"}


# ── attribution (every file edit records who/what did it) ──────────────────
def attribution_path() -> Path:
    return detect.atropos_home() / _ATTRIBUTION


def record_edit(path: str, actor: str = "auto", detail: str = "") -> dict:
    """Append one edit record. actor = 'auto' | 'user' | '<agent>'."""
    p = attribution_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now(), "path": str(path), "actor": actor, "detail": detail}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def attribution_for(path: str, limit: int = 20) -> list:
    """History for one file (newest first)."""
    p = attribution_path()
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("path") == path:
                out.append(rec)
    except OSError:
        return []
    return out[::-1][:limit]


# ── curator (Hermes curator.consolidate) ────────────────────────────────────
def curator_status() -> dict:
    """Weekly scan summary: duplicates, stale, archived."""
    from . import skills as _skills
    all_skills = _skills.list_skills()
    dupes = _find_duplicates(all_skills)
    stale = [s["name"] for s in all_skills if lifecycle(s["name"]) == "stale"]
    return {"ok": True, "total": len(all_skills), "duplicates": dupes,
            "stale": stale, "archived": _archived_count()}


def _find_duplicates(skills: list) -> list:
    """Same-harness skills whose descriptions share >= 80% of tokens."""
    out = []
    for i, a in enumerate(skills):
        for b in skills[i + 1:]:
            ta = set(a.get("description", "").lower().split())
            tb = set(b.get("description", "").lower().split())
            if not ta or not tb:
                continue
            jac = len(ta & tb) / len(ta | tb)
            if jac >= 0.8:
                out.append([a["name"], b["name"]])
    return out


def _archived_count() -> int:
    try:
        return sum(1 for _ in _archive_dir().iterdir()) if _archive_dir().exists() else 0
    except OSError:
        return 0


def curator_run(consolidate: bool = False) -> dict:
    """Run the curator: sweep lifecycle, optionally merge duplicates."""
    transitions = sweep_lifecycle()
    report = curator_status()
    merged = []
    if consolidate:
        for pair in report["duplicates"]:
            keep, drop = pair
            src = skills_dir_for(drop)
            if src.exists():
                shutil.move(str(src), str(_archive_dir() / drop))
                merged.append(drop)
    report["transitions"] = transitions
    report["consolidated"] = merged
    return report


# ── orchestrate (multi-agent, dep ordering, merge) ──────────────────────────
DEFAULT_MERGE_FN = "\n\n".join  # naive join; per-goal merge is agent-defined


def orchestrate(goal: str, subtasks: list, deps: dict = None,
                merge_fns: dict = None) -> dict:
    """Run subtasks across agents in dependency order, merge the results.

    subtasks: [{id, agent, prompt}]. deps: {id: [dep_ids]}.
    Returns {ok, results: {id: result}, merged, order}.
    """
    from . import agents as _agents
    deps = deps or {}
    order = _topo_order([s["id"] for s in subtasks], deps)
    if order is None:
        return {"ok": False, "error": "dependency cycle"}
    results = {}
    for sid in order:
        spec = next(s for s in subtasks if s["id"] == sid)
        res = _agents.run_agent(spec["agent"], spec["prompt"])
        results[sid] = {"ok": bool(res.get("ok")), "result": res.get("reply") or res.get("result")}
    merged = _merge_results(results, goal, merge_fns)
    return {"ok": True, "results": results, "merged": merged, "order": order}


def _topo_order(ids: list, deps: dict):
    """Kahn's algorithm; returns None on cycle."""
    from collections import deque
    indeg = {i: 0 for i in ids}
    adj = {i: [] for i in ids}
    for node, depends in deps.items():
        for d in depends:
            if d not in adj:
                continue
            adj[d].append(node)
            indeg[node] = indeg.get(node, 0) + 1
    q = deque([i for i in ids if indeg[i] == 0])
    order = []
    while q:
        n = q.popleft()
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    return order if len(order) == len(ids) else None


def _merge_results(results: dict, goal: str, merge_fns: dict) -> str:
    fn = (merge_fns or {}).get("x") if merge_fns else None
    if not fn:
        fn = DEFAULT_MERGE_FN
    parts = []
    for sid, r in results.items():
        if r.get("ok") and r.get("result"):
            parts.append(str(r["result"]))
    if not parts:
        return f"(no subtask output for: {goal})"
    return fn(parts)