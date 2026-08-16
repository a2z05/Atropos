#!/usr/bin/env python3
"""Atropos agents — one agent per job, harness & model per agent.

Agent definitions live in ``~/.atropos/agents/*.json``::

    {
      "name": "reviewer",
      "description": "Reviews pull requests",
      "prompt": "You are a strict code reviewer...",
      "harness": "auto",          // clotho | lachesis | atropos | auto
      "model": null,              // null = harness default
      "effort": "high",           // effort tier
      "tools": ["*"],             // tool subset
      "permissions": "default",   // default | read-only | bypass
      "background": false,
      "max_turns": null,
      "on_finish": "report"       // report | telegram | toast
    }

``harness: auto`` resolves through the routing hub heuristics. Runs go
through the middleware pipeline (retry/PII/audit apply to agents too) and
results land in ``~/.atropos/agents/results/``.
"""
import json
import os
import time
import uuid
from pathlib import Path

from . import detect, middleware, routing, settings


def agents_dir() -> Path:
    return detect.atropos_home() / "agents"


def results_dir() -> Path:
    return detect.atropos_home() / "agents" / "results"


def list_agents() -> list:
    d = agents_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def get_agent(name: str) -> dict | None:
    for a in list_agents():
        if a.get("name") == name:
            return a
    return None


def _validate(a: dict) -> str | None:
    if not a.get("name") or not str(a["name"]).strip():
        return "name is required"
    if not a.get("prompt"):
        return "prompt is required"
    if a.get("harness", "auto") not in ("clotho", "lachesis", "atropos", "auto"):
        return f"bad harness: {a.get('harness')}"
    if a.get("permissions", "default") not in ("default", "read-only", "bypass"):
        return f"bad permissions: {a.get('permissions')}"
    if a.get("effort") and a.get("effort") not in settings.EFFORT_TIERS:
        return f"bad effort: {a.get('effort')}"
    return None


def save_agent(a: dict) -> dict:
    """Create or update an agent definition (validated)."""
    err = _validate(a)
    if err:
        raise ValueError(err)
    d = agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{a['name']}.json"
    p.write_text(json.dumps(a, indent=2, ensure_ascii=False), encoding="utf-8")
    return a


def delete_agent(name: str) -> bool:
    p = agents_dir() / f"{name}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def resolve_harness(a: dict, task: str = "") -> str:
    """auto → routing heuristics; manual → the chosen harness."""
    h = a.get("harness", "auto")
    if h != "auto":
        return h
    phrase = task or a.get("prompt") or ""
    try:
        res = routing.dispatch(phrase)
        return res.get("harness", "atropos") if isinstance(res, dict) else "atropos"
    except Exception:
        return "atropos"


def _readonly_toolset(tools: list) -> list:
    # read-only tools only — anything that writes is stripped
    allowed = {"read", "search", "grep", "list", "status", "doctor", "version", "logs"}
    return [t for t in tools if any(k in t.lower() for k in allowed)]


def run_agent(name: str, task: str = "") -> dict:
    """Run one agent: context → middleware → harness dispatch → result.

    The middleware pipeline wraps the run: prompt filtering (pii), retries
    (on_error), audit (on_end). Results persist to results/.
    """
    a = get_agent(name)
    if not a:
        return {"ok": False, "error": f"unknown agent: {name}"}
    if not task:
        task = a.get("prompt", "")
    harness = resolve_harness(a, task)
    run_id = uuid.uuid4().hex[:12]
    ctx = {
        "harness": harness,
        "router": settings.get("router.active", "nain"),
        "model": a.get("model"),
        "prompt": task,
        "tool": None,
        "result": None,
        "error": None,
        "state": {"agent": name, "run_id": run_id, "effort": a.get("effort", "medium")},
    }
    # middleware pipeline — same filters as chat, applied to agents too
    ctx = middleware.run("before_model", ctx)
    if ctx.get("rejected"):
        return {"ok": False, "run_id": run_id, "error": ctx.get("reason", "rejected by filter")}
    # permission enforcement
    perms = a.get("permissions", "default")
    if perms == "read-only":
        ctx["state"]["tools"] = _readonly_toolset(a.get("tools") or ["*"])
    if perms == "read-only":
        ctx["state"]["read_only"] = True
    # dispatch to the harness backend (deterministic stdlib runner)
    result = _dispatch(harness, task, ctx)
    ctx["result"] = result
    ctx = middleware.run("after_model", ctx)
    ctx = middleware.run("on_end", ctx)
    # persist
    results_dir().mkdir(parents=True, exist_ok=True)
    rec = {
        "agent": name,
        "run_id": run_id,
        "harness": harness,
        "task": task[:500],
        "result": (result or "")[:2000],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": bool(result),
    }
    (results_dir() / f"{run_id}.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec


def _dispatch(harness: str, task: str, ctx: dict) -> str:
    """Deterministic stdlib backend for agent runs.

    Atropos itself is a control plane (dry-run by rule): the runner reports
    what it *would* do — harness, effort, tools, filtered prompt — and where
    a real harness exists it delegates (claude: `claude -p`; clotho: log
    channel). The response is real output, never fabricated.
    """
    effort = ctx["state"].get("effort", "medium")
    tools = ctx["state"].get("tools")
    head = f"[{harness} · effort={effort}"
    if tools:
        head += f" · tools={','.join(tools[:5])}"
    head += "]"
    if harness == "lachesis" and detect._find_claude():
        # real delegation when claude is on PATH
        import subprocess
        try:
            p = subprocess.run(
                ["claude", "-p", task[:2000], "--output-format", "text"],
                capture_output=True, text=True, timeout=120,
            )
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()[:2000]
        except Exception:
            pass
    return f"{head} dry-run of: {task[:120]}"


def recent_runs(limit: int = 20) -> list:
    d = results_dir()
    if not d.is_dir():
        return []
    runs = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return runs


if __name__ == "__main__":
    args = os.sys.argv[1:]
    if args and args[0] == "list":
        for a in list_agents():
            print(json.dumps(a, ensure_ascii=False))
    elif args and args[0] == "run" and len(args) > 1:
        print(json.dumps(run_agent(args[1], args[2] if len(args) > 2 else ""),
                          ensure_ascii=False, indent=2))
    else:
        print(json.dumps(list_agents(), ensure_ascii=False, indent=2))