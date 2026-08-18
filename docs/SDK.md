# Atropos SDK (stdlib surface)

Everything a third-party script or another harness needs is pure Python
under `core/` — no API keys beyond your router, no pip. This document is
the stable, tested surface. Anything not listed here is internal and may
move.

## Entry points

| Module | What you get |
|---|---|
| `core.detect` | `atropos_home()` (reads `ATROPOS_HOME` env first), `detect_cloud()` (`railway` / `local` / …) |
| `core.settings` | `get(key, default)` / `set(key, value)` — typed schema, dot-path keys |
| `core.router` | `get()`, `ping()`, failover state |
| `core.chat` | `send(session_id, text)`, `chat_stream(...)`, `session_messages()`, `send_llm(messages)` (OpenAI-style) |
| `core.console` | `run_command(line)` — whitelist-only dispatcher, never free-form shell |
| `core.backup` | `create_backup()`, `list_backups()`, `restore(name)` |
| `core.approve` | `check_all_command_guards(cmd, env)`, `detect_dangerous_command(cmd)`, `request_tool_approval(...)` |
| `core.skills` | `list_skills()`, `skill_lint(name)`, `skill_view(name)`, `sync_to_hermes()` |
| `core.migrate` | `import_plan(source, kinds)`, `import_apply(source, kinds, yes)`, `undo(yes)` |
| `core.guest` | `record_sealed(user, text)`, `sealed_owner_view()` (counts only) |
| `core.middleware` | `run(hook, ctx)`, `catalog()`, `set_enabled(name, on)` |
| `core.tools` | the tool shims (`kanban_*`, `docs`, `tts`, …) — CLI parity surface |

## The middleware context dict

Every filter receives and returns one dict. Recognized keys:

```python
{
  "prompt":  str,             # the outgoing prompt (before_model)
  "result":  str,             # the model reply (after_model)
  "tool":    {"name": ..., "args": ..., "command": ..., "workflow": ...},
  "harness": "claude|hermes|atropos",
  "router":  "nain|omni|local",
  "model":   str,
  "error":   str | None,
  "state":   dict,            # per-session scratch (filters may write)
}
```

To **block**: return `{**ctx, "rejected": True, "reason": "..."}` — the
pipeline short-circuits and the reason becomes the error message.

## Approval decision shape

`check_all_command_guards` / `check_dangerous_command` / `request_tool_approval`
all return the same shape:

```python
{"approved": True, "message": None}                        # allowed
{"approved": False, "message": "BLOCKED (hardline): ...",
 "hardline": True}                                          # unconditional
{"approved": False, "message": "...", "user_deny": True}    # approvals.deny
{"approved": False, "status": "pending_approval", ...}      # queued for human
```

Order of checks (matches Hermes): container skip → hardline floor → sudo
stdin guard → user deny rules → yolo / `approvals.mode: off` → permanent
allowlist → smart approval (mode=smart) → human/gateway/cron decision.

## Hermetic testing

Tests swap the home via `ATROPOS_HOME` env (not by patching
`detect._home`). Example:

```python
import os, tempfile
os.environ["ATROPOS_HOME"] = tempfile.mkdtemp(prefix="test_")
from core import settings
```

All tests are `unittest`, stdlib-only, and hermetic — see `tests/`.
