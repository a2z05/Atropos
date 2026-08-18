# Atropos Plugins & Filters

Atropos exposes two extension layers — **middleware filters** (hooks that
transform the prompt/reply pipeline) and the **plugin catalog** (named,
toggleable extensions). Both are stdlib-only: no pip packages, no build step.

## Middleware filters

Filters sit between you and the AI. Each one has a human name, a hook, and
a body that receives the pipeline context dict and returns it (mutated or
rejected).

| Hook | When it runs |
|---|---|
| `before_model` | just before the prompt is sent to the router |
| `after_model` | just after the reply comes back |
| `before_tool` | before a tool call executes |
| `after_tool` | after a tool call finishes |
| `on_start` / `on_end` | session lifecycle |
| `on_error` | when a request fails |

### Built-in filters (19)

`pii` · `retry` · `summary` · `approval` · `audit` · `translate` ·
`spellcheck` · `code_guard` · `tone` · `context` · `length` · `brand` ·
`ratelimit` · `webfetch_cache` · `followup` · `json_mode` · `rollback` ·
`budget` · `guest_guard`

The **`approval` filter** (v18) is the ported Hermes dangerous-command gate:
when enabled it runs every flagged tool command through the 77-pattern
detector + 12-pattern hardline floor from `core/approve.py`. Hardline
commands (`rm -rf /`, `mkfs`, `dd` to a raw device, shutdown/reboot, fork
bomb) are blocked unconditionally — never bypassable, not even with
`approvals.mode: off`. See `docs/PARITY.md` and `core/approve.py`.

### Custom filters

Dropping a file into `~/.atropos/custom_filters/` adds a filter:

- **YAML** — declarative rules: `match` regex + `action: replace|append|block|transform`.
- **Python** — a module exposing `def filter(ctx) -> ctx`. Imported through
  an AST sandbox gate; dangerous imports are skipped with an error surfaced
  in the filter list.

### CLI

```
atropos middleware list                    # catalog + on/off state
atropos middleware on approval             # enable the approval gate
atropos middleware off pii
atropos middleware order audit pii context # reorder
```

## Plugin lifecycle

Plugins are named extensions with enable/disable/remove:

```
atropos plugin list
atropos plugin enable <name>
atropos plugin disable <name>
atropos plugin remove <name>
```

A plugin's hooks receive the same context dict as filters and may set
`rejected: True` with a `reason` to block a prompt or tool call.

## Approval escalation

Any filter or plugin can escalate a tool call to the human gate instead of
vetoing it:

```python
from core import approve
result = approve.request_tool_approval(
    "write_file", "writing into ~/.ssh", rule_key="ssh-write")
```

`request_tool_approval` reuses the same session/permanent allowlist,
timeout and deny messaging as dangerous-command detection, and **fails
closed** when no interactive user or gateway is present. Gateway sessions
(Telegram etc.) can register a notify callback
(`approve.register_gateway_notify(session_key, cb)`) and answer via
`approve.resolve_gateway_approval(session_key, choice)`.

See also: `docs/SDK.md` for the full context-dict contract.
