#!/usr/bin/env python3
"""Atropos delegation — spawn one or more agents for a goal or a batch.

Ported from hermes-agent/tools/delegate_tool.py (task normalization,
role normalization, concurrency cap, focused child system-prompt packing,
per-child result shaping, batch aggregation) with batch-unit semantics from
hermes-agent/tools/async_delegation.py (one consolidated result per fan-out,
one capacity cap) and structured-output parsing/validation from
hermes-agent/agent/plugin_llm.py (_strip_code_fences / _parse_structured_text).
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from . import agents, settings

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENT_CHILDREN = 3
# Sentinel a harness emits when it gives up with repeated empty responses;
# mirror of delegate_tool._run_single_child "(empty)" handling.
_EMPTY_SENTINEL = "(empty)"
# Tools a child must never have access to (delegate_tool.DELEGATE_BLOCKED_TOOLS
# minus the ones Atropos has no equivalent of).  Enforced at prompt level:
# the deterministic runner has no tool gate, so the packed child prompt
# states the exclusions instead.
_BLOCKED_CHILD_TOOLS = ("delegate", "clarify", "memory", "send_message", "cronjob")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)


def _normalize_role(r: Optional[str]) -> str:
    """Normalize a caller-provided role to 'leaf' or 'orchestrator'.

    None/empty -> 'leaf' (delegate_tool._normalize_role); unknown strings
    coerce to 'leaf' with a warning log.
    """
    if r is None or not r:
        return "leaf"
    r_norm = str(r).strip().lower()
    if r_norm in {"leaf", "orchestrator"}:
        return r_norm
    logger.warning("Unknown delegate role=%r, coercing to 'leaf'", r)
    return "leaf"


def _max_concurrent_children() -> int:
    """Concurrency cap: delegation.max_concurrent_children config > env
    DELEGATION_MAX_CONCURRENT_CHILDREN > default 3 (floor 1), mirroring
    delegate_tool._get_max_concurrent_children priority and bounds."""
    raw = settings.get("delegation.max_concurrent_children")
    if raw is not None:
        try:
            result = max(1, int(raw))
            if result > 10:
                logger.warning(
                    "delegation.max_concurrent_children=%d: each child consumes "
                    "tokens independently; high values multiply cost linearly.",
                    result,
                )
            return result
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_concurrent_children=%r is not a valid integer; "
                "using default %d", raw, _DEFAULT_MAX_CONCURRENT_CHILDREN)
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    env_val = __import__("os").environ.get("DELEGATION_MAX_CONCURRENT_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    return _DEFAULT_MAX_CONCURRENT_CHILDREN


def _recover_tasks_from_json_string(tasks: Any) -> tuple:
    """Accept batch as a JSON array string, else pass through (delegate_tool.
    _recover_tasks_from_json_string; same error wording)."""
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    role: str = "leaf",
    max_spawn_depth: int = 2,
    child_depth: int = 1,
) -> str:
    """Focused system prompt for a child agent (delegate_tool.
    _build_child_system_prompt).  When role='orchestrator' appends a
    delegation-capability block; the depth note is literal truth.
    Atropos-specific: the blocked-tools line replaces Hermes's toolset
    stripping because the deterministic runner has no tool gate."""
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Keep your final summary tight: lead with outcomes, prefer bullet "
        "points over paragraphs, and don't replay your whole process. Your "
        "response is returned to the parent agent as a summary, and overlong "
        "summaries crowd out the parent's context window."
    )
    if role != "orchestrator":
        # Orchestrators keep the delegate capability (matching Hermes, where
        # only the leaf role strips the explicit delegation toolset — here the
        # restriction is stated at prompt level instead).
        parts.append(
            "\nBlocked tools: you do NOT have access to "
            + ", ".join(_BLOCKED_CHILD_TOOLS) + "."
        )
    if role == "orchestrator":
        child_note = (
            "Your own children MUST be leaves (cannot delegate further) "
            "because they would be at the depth floor — you cannot pass "
            "role='orchestrator' to your own delegate calls."
            if child_depth + 1 >= max_spawn_depth
            else "Your own children can themselves be orchestrators or leaves, "
            "depending on the `role` you pass to delegate."
        )
        parts.append(
            "\n## Subagent Spawning (Orchestrator Role)\n"
            "You CAN spawn your own subagents to parallelize independent work.\n\n"
            "WHEN to delegate:\n"
            "- The goal decomposes into 2+ independent subtasks that can "
            "run in parallel.\n"
            "- A subtask is reasoning-heavy and would flood your context "
            "with intermediate data.\n\n"
            "WHEN NOT to delegate:\n"
            "- Single-step mechanical work — do it directly.\n"
            "- Trivial tasks you can execute in one or two tool calls.\n"
            "- Re-delegating your entire assigned goal to one worker "
            "(that's just pass-through with no value added).\n\n"
            "Coordinate your workers' results and synthesize them before "
            "reporting back to your parent. You are responsible for the "
            "final summary, not your workers.\n\n"
            f"NOTE: You are at depth {child_depth}. The delegation tree "
            f"is capped at max_spawn_depth={max_spawn_depth}. {child_note}"
        )
    return "\n".join(parts)


def _strip_code_fences(text: str) -> str:
    """Pull the first fenced code block out of ``text`` if any; returns
    ``text`` unchanged when no fence is present (plugin_llm._strip_code_fences)."""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _validate_against_schema(value: Any, schema: Dict[str, Any], path: str = "$") -> None:
    """Minimal JSON-Schema subset validator.

    DELIBERATE DEVIATION: Hermes skips validation when the optional
    jsonschema package is absent (plugin_llm._parse_structured_text); Atropos
    is pure-stdlib, so jsonschema would never be present and the check would
    silently no-op.  This subset (type incl. unions, required, properties,
    items, enum) covers the common structured-output shapes and raises a
    ValueError with the same message pattern as the Hermes path.
    """
    if schema is None:
        return
    if not isinstance(schema, dict):
        return

    def _type_of(v: Any) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, int):
            return "integer"
        if isinstance(v, float):
            return "number"
        if isinstance(v, str):
            return "string"
        if isinstance(v, list):
            return "array"
        if isinstance(v, dict):
            return "object"
        return "string"

    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if _type_of(value) not in types and not (
            "number" in types and _type_of(value) == "integer"
        ):
            raise ValueError(
                f"{path}: expected type {t}, got {_type_of(value)}"
            )

    if isinstance(value, dict):
        for req in schema.get("required") or []:
            if req not in value:
                raise ValueError(f"{path}: missing required property '{req}'")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                _validate_against_schema(value[key], sub, f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate_against_schema(item, schema["items"], f"{path}[{i}]")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise ValueError(f"{path}: value is not one of: {enum}")


def _parse_structured_text(text: str, json_schema: Any) -> tuple:
    """Return ``(parsed, content_type)``; ``content_type`` is ``"json"``
    when parsing succeeded and validation passed, ``"text"`` otherwise
    (plugin_llm._parse_structured_text).  Raises ValueError when the parsed
    value fails schema validation, mirroring the Hermes behavior."""
    if json_schema is None:
        return None, "text"
    if not text:
        return None, "text"
    try:
        parsed = json.loads(_strip_code_fences(text))
    except (json.JSONDecodeError, ValueError):
        return None, "text"
    _validate_against_schema(parsed, json_schema)
    return parsed, "json"


def _run_single(
    task_index: int,
    goal: str,
    agent_name: str,
    task: str,
    output_schema: Any = None,
) -> Dict[str, Any]:
    """Run one child agent and shape the result like delegate_tool.
    _run_single_child's entry: status completed/failed, exit_reason, summary,
    tokens, tool_trace, duration_seconds; the "(empty)" sentinel and empty
    output count as failure."""
    start = time.monotonic()
    rec = agents.run_agent(agent_name, task)
    summary = rec.get("result") or ""
    api_calls = 0
    duration = round(time.monotonic() - start, 2)

    if rec.get("ok") is False:
        status, exit_reason, error = "failed", "error", rec.get("error") or (
            "Subagent did not produce a response.")
    elif not summary or summary.strip() == _EMPTY_SENTINEL:
        status, exit_reason, error = "failed", "error", (
            "Subagent did not produce a response.")
    else:
        status, exit_reason, error = "completed", "completed", None

    entry: Dict[str, Any] = {
        "task_index": task_index,
        "status": status,
        "summary": summary,
        "api_calls": api_calls,
        "duration_seconds": duration,
        "model": rec.get("model"),
        "exit_reason": exit_reason,
        "tokens": {"input": 0, "output": 0},
        "tool_trace": [],
        "goal": goal,
        "agent": agent_name,
        "run_id": rec.get("run_id"),
    }
    if error:
        entry["error"] = error
    if output_schema is not None:
        try:
            parsed, ctype = _parse_structured_text(summary, output_schema)
        except ValueError as exc:
            entry["status"] = "failed"
            entry["exit_reason"] = "schema_error"
            entry["error"] = f"Structured output did not match schema: {exc}"
        else:
            entry["output"] = parsed
            entry["output_parsed"] = ctype == "json"
    return entry


def _ephemeral_agent(goal: str, context: Optional[str], role: str) -> str:
    """Create (and persist) a focused child agent in the agents/ registry.

    DELIBERATE DEVIATION: Hermes builds child agents in memory
    (delegate_tool._build_child_agent).  Atropos executes children through
    core/agents.py run_agent, which requires a saved definition; the
    ephemeral agent is persisted so runs stay auditable via
    agents.recent_runs().  The packed child system prompt becomes the
    agent's identity prompt, exactly the content Hermes would use as the
    child's system prompt.
    """
    name = "delegate-" + (
        re.sub(r"\W+", "-", goal[:20]).strip("-").lower() or "delegate")
    agents.save_agent({
        "name": name,
        "description": "spawned delegation",
        "prompt": _build_child_system_prompt(goal, context, role=role),
        "harness": "auto",
        "model": None,
        "effort": "medium",
        "tools": ["*"],
        "permissions": "default",
    })
    return name


def _delegate(
    goal: Optional[str] = None,
    agent: Optional[str] = None,
    batch: Optional[Any] = None,
    context: Optional[str] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    role: Optional[str] = None,
) -> dict:
    """Core of delegate(); kept separate so all errors collapse into the
    {ok, error} envelope once, at the public boundary."""
    top_role = _normalize_role(role)

    # Normalize to a task list (delegate_task): batch array (or JSON-array
    # string) wins; otherwise a single goal+context task.
    recovered, tasks_error = _recover_tasks_from_json_string(batch)
    if tasks_error:
        return {"ok": False, "error": tasks_error}
    if recovered is not None:
        batch = recovered

    max_children = _max_concurrent_children()
    if isinstance(batch, list):
        if len(batch) > max_children:
            return {"ok": False, "error": (
                f"Too many tasks: {len(batch)} provided, but "
                f"max_concurrent_children is {max_children}. "
                f"Either reduce the task count, split into multiple "
                f"delegate calls, or raise delegation.max_concurrent_children "
                f"in config.")}
        task_list = batch
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [{"goal": goal, "context": context}]
    else:
        return {"ok": False, "error":
                "Provide either 'goal' (single task) or 'tasks' (batch)."}

    if not task_list:
        return {"ok": False, "error": "No tasks provided."}

    for i, task in enumerate(task_list):
        if isinstance(task, str):
            task_list[i] = {"goal": task}
            task = task_list[i]
        if not isinstance(task, dict):
            return {"ok": False, "error":
                    f"Task {i} must be an object, got {type(task).__name__}."}
        if not task.get("goal", "").strip():
            return {"ok": False, "error":
                    f"Task {i} is missing a 'goal'."}

    overall_start = time.monotonic()
    results: List[Dict[str, Any]] = []
    for i, task in enumerate(task_list):
        t_goal = task["goal"]
        t_role = _normalize_role(task.get("role") or top_role)
        t_context = task.get("context") if task.get("context") is not None else (
            context if len(task_list) == 1 else None)
        t_agent = task.get("agent") or agent
        if t_agent:
            # Named-agent path (Atropos extension; Hermes always builds a
            # fresh child): the agent's own prompt is authoritative, context
            # rides along on the task text.
            task_text = t_goal
            if t_context:
                task_text = f"{t_goal}\n\nCONTEXT:\n{t_context}"
            results.append(_run_single(i, t_goal, t_agent, task_text,
                                       output_schema))
        else:
            name = _ephemeral_agent(t_goal, t_context, t_role)
            results.append(_run_single(i, t_goal, name, t_goal, output_schema))

    duration = round(time.monotonic() - overall_start, 2)
    combined = {"results": results, "total_duration_seconds": duration}
    # Batch status rule from async_delegation.dispatch_async_delegation_batch
    # worker: completed unless every child failed/interrupted.
    if results and all(r.get("status") not in ("completed",) for r in results):
        combined["error"] = "All delegated tasks failed"
        ok = False
    else:
        ok = True

    if len(task_list) == 1:
        entry = results[0]
        return {
            "ok": entry["status"] == "completed",
            "result": entry,
            "agent": agent,
            "output": entry.get("output") if output_schema is not None else None,
            "summary": entry.get("summary"),
            "total_duration_seconds": duration,
        }
    return {
        "ok": ok,
        "result": combined,
        "agent": agent,
        "batch_results": results,
        "total_duration_seconds": duration,
    }


def list_delegations(limit: int = 50) -> list:
    """Recent delegation runs, newest first (audit view over the same
    results/ store core.agents uses — Atropos's in-memory-records analogue
    of async_delegation's durable state table)."""
    runs = agents.recent_runs(limit=limit)
    return [r for r in runs if r.get("agent", "").startswith("delegate-")]


def delegate(
    goal: Optional[str] = None,
    agent: Optional[str] = None,
    batch: Optional[Any] = None,
    context: Optional[str] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    role: Optional[str] = None,
) -> dict:
    """Spawn one or more agents to handle a delegated goal or batch.

    Single mode: ``delegate(goal, context=..., role=...)``.  Batch mode:
    ``delegate(batch=[{goal, context, role?, agent?}, ...])`` — the whole
    fan-out runs and returns ONE consolidated result (async_delegation
    batch-unit semantics).  ``output_schema`` validates the child summary
    as structured JSON (plugin_llm parsing rules: code fences stripped,
    then validated against the schema subset).

    Always returns ``{ok: ..., error: ...}``; errors never raise.
    """
    try:
        return _delegate(goal=goal, agent=agent, batch=batch, context=context,
                         output_schema=output_schema, role=role)
    except Exception as exc:
        logger.exception("delegate failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(delegate(goal="summarize the release notes"), ensure_ascii=False))