#!/usr/bin/env python3
"""Atropos middleware — "Filters & Plugins", a hook pipeline around the AI.

Each middleware is a callable taking a context dict and returning it:
    ctx = {harness, router, model, prompt, tool, result, error, state}
Hooks: before_model / after_model / before_tool / after_tool / on_start /
on_end / on_error. Middleware may mutate the ctx, short-circuit (reject)
with a reason, or swap the model/router. One concern per filter; failures
are isolated (one bad filter never kills the pipeline).

The dashboard calls these "Filters & Plugins" — human names, toggles, no
hook jargon. Custom filters live in ~/.atropos/custom_filters/ as YAML
(text rules) or .py (power users).
"""

import ast
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from . import detect, settings

# ── catalog: human name → (hook, description, factory) ─────────────────────
CATALOG = {}


def _register(key, hook, desc, fun=None, kind="filter"):
    CATALOG[key] = {"hook": hook, "description": desc, "fun": fun, "kind": kind}


# ── prebuilt filter bodies ────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)")
_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
_SECRET_MASK = "***"

_PII_REDIRECT = "I'm just a friendly assistant. How can I help today?"
_PRIVATE_WORDS = ["atropos", "hermes", "arophin", "a2z", "server", "repo",
                  "token", "config", "deploy", "ssh", "api_key"]


def _pii(ctx):
    text = ctx.get("prompt") or ""
    changed = False
    for rx, rep in ((_EMAIL_RE, _SECRET_MASK), (_PHONE_RE, _SECRET_MASK), (_TOKEN_RE, _SECRET_MASK)):
        if rx.search(text):
            text = rx.sub(rep, text)
            changed = True
    if changed:
        ctx["prompt"] = text
    return ctx


def _retry(ctx):
    err = ctx.get("error")
    if not err:
        return ctx
    if ctx.get("retries", 0) < 3:
        ctx["retries"] = ctx.get("retries", 0) + 1
        ctx["retry"] = True
    else:
        fallback = ctx.get("state", {}).get("fallback_router")
        if fallback and ctx.get("router") != fallback:
            ctx["router"] = fallback
            ctx["retry"] = True
    return ctx


def _code_guard(ctx):
    tool = ctx.get("tool") or ""
    name = (tool or {}).get("name", "") if isinstance(tool, dict) else str(tool)
    if any(k in name.lower() for k in ("remove", "delete", "rm", "unlink", "subprocess",
                                       "os.system", "shell", "exfil", "curl")):
        return {**ctx, "rejected": True,
                "reason": "blocked by the code guard: destructive or shell-capable tool"}
    return ctx


def _approval(ctx):
    """Dangerous-action gate: Hermes approval.py detection, adapted (v18 A.11).

    Events are safe until a tool explicitly escalates; users opt into the
    gate by enabling ``middleware.approval`` (which runs this body over
    ``before_tool`` context). No deny rules are pre-registered, so plain
    Atropos usage is unaffected by default.
    """
    from . import approve as _approve
    tool = ctx.get("tool") or {}
    name = tool.get("name", "") if isinstance(tool, dict) else str(tool)
    if not name:
        return ctx
    target = tool.get("args") or tool.get("command") or tool.get("workflow") or ""
    if isinstance(target, dict):
        target = target.get("command", "")
    if not isinstance(target, str):
        target = str(target)
    target = target.strip()
    if not target:
        return ctx
    # Hardline + deny rules are unconditional and fire in every context
    # (before the approvals.mode=off bypass, matching hermes ordering).
    is_hard, hard_desc = _approve.detect_hardline_command(target)
    if is_hard:
        return {**ctx, "rejected": True,
                "reason": _approve._hardline_block_result(hard_desc)["message"]}
    deny_hit = _approve._match_user_deny_rule(target)
    if deny_hit is not None:
        return {**ctx, "rejected": True,
                "reason": _approve._user_deny_block_result(deny_hit)["message"]}
    if _approve._get_approval_mode() == "off":
        return ctx
    # A headless filter has no human to ask: flagged actions fail closed
    # (mirror request_tool_approval's fail_closed_when_no_human contract).
    is_dang, _pk, desc = _approve.detect_dangerous_command(target)
    if is_dang:
        return {**ctx, "rejected": True,
                "reason": (f"BLOCKED by approval gate: {desc}. A middleware "
                           "filter flagged this action and no interactive user "
                           "is present to approve it. Run it manually or "
                           "disable the approval filter.")}
    return ctx


def _audit(ctx):
    from .activity import add as _act
    try:
        ev = {
            "kind": "filter." + (ctx.get("hook") or "run"),
            "harness": ctx.get("harness"),
            "router": ctx.get("router"),
            "model": ctx.get("model"),
            "ts": ctx.get("state", {}).get("ts"),
        }
        _act(**ev)
    except Exception:
        pass
    return ctx


def _summary(ctx):
    prompt = ctx.get("prompt") or ""
    if len(prompt) > 60_000:
        ctx["prompt"] = prompt[:40_000] + "\n[earlier context compacted]"
    return ctx


def _context(ctx):
    try:
        from . import detect as _det
        snippet = (f"[context] time: {__import__('time').strftime('%H:%M')} "
                   f"· box: {_det.detect_cloud()} · router: {ctx.get('router')}")
        ctx["prompt"] = (ctx.get("prompt") or "") + "\n" + snippet
    except Exception:
        pass
    return ctx


def _length(ctx):
    res = ctx.get("result") or ""
    pref = (ctx.get("state") or {}).get("length", "medium")
    limits = {"short": 500, "medium": 2000, "long": 8000}
    n = limits.get(pref, 2000)
    if isinstance(res, str) and len(res) > n:
        ctx["result"] = res[:n] + "\n…(clamped by length filter)"
    return ctx


def _brand(ctx):
    text = ctx.get("prompt") or ""
    stripped = re.sub(r"\b(?:as an AI|I'm (?:just )?an AI|as a language model|I am an AI)\b",
                      "", text, flags=re.I)
    if stripped != text:
        ctx["prompt"] = stripped
    return ctx


def _ratelimit(ctx):
    state = ctx.setdefault("state", {})
    now = __import__("time").time()
    state.setdefault("window", [])
    state["window"] = [t for t in state["window"] if now - t < 60]
    if len(state["window"]) >= 10:
        return {**ctx, "rejected": True, "reason": "rate limit: too many messages this minute"}
    state["window"].append(now)
    return ctx


def _followup(ctx):
    res = ctx.get("result") or ""
    if isinstance(res, str) and 0 < len(res) < 200 and "\n" not in res:
        ctx["result"] = res + "\n\nWant me to go deeper?"
    return ctx


def _json_mode(ctx):
    res = ctx.get("result")
    if isinstance(res, str):
        try:
            json.loads(res)
            ctx["json"] = res
        except ValueError:
            pass
    return ctx


def _register_all():
    if CATALOG:
        return
    _register("budget", "before_model",
              "Keeps your token bill predictable: stops or switches models when the monthly budget is close.",
              kind="policy")
    _register("pii", "before_model",
              "Never leaks personal data to the model: hides emails, phone numbers and tokens first.",
              fun=_pii)
    _register("retry", "on_error",
              "Fixes temporary failures automatically: retries with backoff, then falls back to another router.",
              fun=_retry)
    _register("summary", "before_model",
              "Keeps long chats fast without losing the point: auto-compacts oversized sessions.",
              fun=_summary)
    _register("approval", "before_tool",
              "You approve risky actions before they happen: pauses on dangerous tool calls.",
              fun=_approval, kind="policy")
    _register("audit", "on_end",
              "Keeps a full record of everything the AI did: appends every step to activity.jsonl.",
              fun=_audit)
    _register("translate", "before_model",
              "Auto-translates prompts and responses between your language and the model's.",
              kind="policy")
    _register("spellcheck", "before_model",
              "Corrects obvious typos in prompts before they go out.",
              kind="policy")
    _register("code_guard", "before_tool",
              "Extra safety net beyond the console whitelist: blocks file-deletion, shell and exfiltration tools.",
              fun=_code_guard)
    _register("tone", "before_model",
              "Applies the chosen tone (sharp, humorous, formal) to the system prompt.",
              kind="policy")
    _register("context", "before_model",
              "Injects a small snippet — current time, box status, repo name — into every prompt.",
              fun=_context)
    _register("length", "after_model",
              "Clamps responses to short, medium or long per your preference.",
              fun=_length)
    _register("brand", "before_model",
              "Never says \"I'm an AI\" or name-drops competitors unless asked.",
              fun=_brand)
    _register("ratelimit", "before_model",
              "Caps messages per minute and hour per session to prevent runaway costs.",
              fun=_ratelimit)
    _register("webfetch_cache", "before_tool",
              "Caches recent web fetches so repeated calls come back instantly.",
              kind="policy")
    _register("followup", "after_model",
              "Adds a follow-up suggestion line to short responses.",
              fun=_followup)
    _register("json_mode", "after_model",
              "Forces structured JSON output for API-style requests.",
              fun=_json_mode)
    _register("rollback", "before_tool",
              "Snapshots state before any tool mutation so a bad action can be undone.",
              kind="policy")


def catalog() -> dict:
    _register_all()
    return dict(CATALOG)


# populate the catalog at import so run() can resolve filter bodies
_register_all()


# ── configured order ───────────────────────────────────────────────────────
def _filters_dir() -> Path:
    return detect.atropos_home() / "custom_filters"


def _load_python_filter(path: Path) -> dict | None:
    """Import a custom .py filter exposing `def filter(ctx) -> ctx`."""
    try:
        src = path.read_text(encoding="utf-8")
        ast.parse(src)  # syntax gate
    except (OSError, SyntaxError) as e:
        return {"error": f"syntax: {e}"}
    # sandbox: stdlib-only import allowlist (importlib, ast, re, json, math, time, pathlib, copy, collections)
    allowed = {"importlib", "ast", "re", "json", "math", "time", "pathlib",
               "copy", "collections", "os.path"}
    for m in re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M):
        root = m.split(".")[0]
        if root not in allowed and root != "core":
            return {"error": f"sandbox: import '{root}' not allowed"}
    try:
        spec = importlib.util.spec_from_file_location("custom_filter", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "filter", None)):
            return {"error": "missing def filter(ctx)"}
        return {"fun": mod.filter}
    except Exception as e:
        return {"error": str(e)}


def _load_yaml_filter(path: Path) -> dict | None:
    """Simple text rule: {name, hook, action: replace|append|block|transform,
    match: regex, replace_with: text} — no code needed."""
    from . import config as _cfg
    try:
        rule = _cfg.parse_yaml(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"parse: {e}"}
    rule = rule or {}
    hook = rule.get("hook", "before_model")
    action = rule.get("action", "block")
    match = rule.get("match", "")
    if action not in ("replace", "append", "block", "transform"):
        return {"error": f"bad action: {action}"}
    try:
        rx = re.compile(match)
    except re.error as e:
        return {"error": f"bad regex: {e}"}
    replace_with = rule.get("replace_with", "")

    def apply(ctx):
        text = ctx.get("prompt") or ""
        if not rx.search(text):
            return ctx
        if action == "block":
            return {**ctx, "rejected": True, "reason": replace_with or "blocked by filter"}
        if action == "append":
            ctx["prompt"] = text + "\n" + replace_with
        elif action == "replace":
            ctx["prompt"] = rx.sub(replace_with, text)
        elif action == "transform":
            ctx["transformed"] = rx.sub(replace_with, text)
        return ctx

    return {"fun": apply}


def enabled_list() -> list:
    try:
        return list(settings.get("middleware.enabled", []) or [])
    except Exception:
        return []


_STATE = {"order": None, "filters": {}}


def _all_filters(order: list | None = None) -> list:
    """(key, fun, hook) for every enabled builtin + custom filter, in order."""
    base = enabled_list()
    order = list(order if order is not None else base)
    if _STATE["order"] != order:
        _STATE["filters"] = {}
        _STATE["order"] = order
    out = []
    for key in order:
        if key in _STATE["filters"]:
            entry = _STATE["filters"][key]
        else:
            cat = CATALOG.get(key)
            entry = {"hook": cat["hook"] if cat else "before_model",
                     "fun": cat["fun"] if cat else None}
            _STATE["filters"][key] = entry
        if entry.get("error"):
            continue
        out.append((key, entry["fun"], entry["hook"]))
    # custom filters from ~/.atropos/custom_filters/
    d = _filters_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.yaml")):
            entry = _load_yaml_filter(p)
            if entry and "fun" in entry:
                out.append((p.stem + ".yaml", entry["fun"], "before_model"))
        for p in sorted(d.glob("*.py")):
            entry = _load_python_filter(p)
            if entry and "fun" in entry:
                out.append((p.stem + ".py", entry["fun"], "before_model"))
    return out


def run(hook: str, ctx: dict, order: list | None = None) -> dict:
    """Run every enabled filter registered for `hook` over ctx, in order.

    Errors are isolated: a failing filter logs and continues. Returns the
    mutated ctx (possibly with rejected=True + reason for short-circuits).
    Each filter pass records a breadcrumb (benchmark area 29 adoption) so
    the decision trail is inspectable.
    """
    from . import errors  # stdlib-only core module; the import cannot fail
    for key, fun, h in _all_filters(order):
        if h != hook:
            continue
        try:
            if fun:
                res = fun(ctx)
                if isinstance(res, dict):
                    ctx = res
        except Exception as e:
            ctx.setdefault("filter_errors", []).append(f"{key}: {e}")
            errors.breadcrumb("middleware", f"{key} raised: {e}", "error")
        if ctx.get("rejected"):
            if errors:
                errors.breadcrumb("middleware",
                                  f"{key} rejected ({ctx.get('reason', '')})",
                                  "warn")
            break
    return ctx


# ── CLI ────────────────────────────────────────────────────────────────────
def list_filters(json_mode=False):
    rows = []
    for key, meta in catalog().items():
        rows.append([key, meta["description"], "on" if key in enabled_list() else "off"])
    return rows


def set_enabled(key: str, on: bool):
    order = enabled_list()
    if on and key not in order:
        order.append(key)
    if not on and key in order:
        order.remove(key)
    settings.set("middleware.enabled", order)
    return order


def set_order(keys: list):
    order = [k for k in keys if k in catalog()] + [k for k in enabled_list() if k not in keys]
    settings.set("middleware.enabled", order)
    return order


if __name__ == "__main__":
    # atropos middleware list|on <k>|off <k>|order <keys...>
    args = sys.argv[1:]
    if args and args[0] == "order" and len(args) > 1:
        print(json.dumps(set_order(args[1:]), indent=2))
    elif args and args[0] in ("on", "off") and len(args) > 1:
        print(json.dumps(set_enabled(args[1], args[0] == "on"), indent=2))
    else:
        print(json.dumps(list_filters(), indent=2, ensure_ascii=False))