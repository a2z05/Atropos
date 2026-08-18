# FUSION — how the two harnesses become one

Atropos fuses **Hermes Agent** (orchestration, comms, memory, persistence)
with **Claude Code** (coding, debugging, surgery). This document maps the
fusion points — where Atropos reads, writes, or mirrors a Hermes/Claude
surface, and where it deliberately deviates.

## Identity

| Surface | Canonical store | Mode |
|---|---|---|
| SOUL.md / AGENTS.md / SYSTEM.md / GUEST.md / CODE_STYLE.md | `~/.atropos/identity/` | shared · per-harness · atropos-only |
| prompts/ | `~/.atropos/identity/prompts/` | per-file |

Every write is hash-guarded; conflicts are resolved with
`overwrite|keep|diff` and every save snapshots a version history
(`identity snapshots`, `restore`).

## Config

| File | Mirrored in | Validation |
|---|---|---|
| `hermes.yaml` | `~/.atropos/conflayer/hermes.yaml` | YAML (line+col errors) |
| `hermes.env` | `~/.atropos/conflayer/hermes.env` | `.env` grammar |
| `claude.settings.json` | `~/.atropos/conflayer/claude.settings.json` | JSON |
| `claude.mcp.json` | `~/.atropos/conflayer/claude.mcp.json` | JSON |
| `router.yaml` | `~/.atropos/conflayer/router.yaml` | YAML |
| `atropos.yaml` | `~/.atropos/conflayer/atropos.yaml` | YAML |

The same 3-mode core law applies; snapshots + rollback on every edit.

## MCP / models / webhooks / commands

- **MCP**: canonical `~/.atropos/mcp_servers.json`, discovery from both
  harnesses, ask-first `adopt`, live probes, secret-ref projection.
- **Models**: `~/.atropos/models.json` with per-harness assignment.
- **Webhooks**: event registry (`core/webhooks.py`), per-hook error isolation.
- **Commands**: `commands.json` with `mode: hermes|claude|atropos` routing.

## Memory

Hermes `state.db` memory is served read-only (`memory add` writes Atropos'
own store; the Hermes DB is never mutated). Sealed guest memory (v18 K)
never leaks content into owner views.

## Skills (v18 I)

Hermes skills live in the **nested** layout `skills/<category>/<name>/SKILL.md`.
Atropos reads both flat and nested layouts, lints frontmatter against Hermes
budgets (description ≤ 1024 chars, content ≤ 100k chars, prompt ≤ 60 chars),
matches platform/environment (`skills.environments`), and can `--sync` /
`--export` back into the Hermes store. Provenance + usage tracking feed the
auto-improve curator (v18 F).

## Migration (v18 J)

`atropos migrate plan|apply|undo` imports Hermes state (config / memory /
skills) with the ask-first contract: the plan is a pure dry-run, apply
requires `--yes`, every import snapshots to `backups/migrate_<ts>/` and
logs `migrations.jsonl`. `undo` restores the snapshot and removes imported
skills — revertible by construction.

## Approval (v18 A.11)

The dangerous-command gate is a direct port of Hermes `approval.py`
(77 dangerous + 12 hardline patterns, byte-verified). Deviations are
documented in the module docstring: settings-backed config, an extra
`~/.atropos` home fold, no tirith scanner, plain `input()` prompting.

## Parity

`docs/PARITY.md` is the living matrix; `tests/test_parity.py` enforces
that every claimed CLI command exists and runs. `tests/test_copy_parity.py`
locks the ported modules' behavior to their Hermes sources.
