# Atropos v1.4.0 Final Polish — Design

Date: 2026-08-15 · Status: approved (owner-authored brief, post-v1.4.0)
Branches: never in worktree (in-place edits) · Base: f80ef6d (v1.4.0)

## Scope

The owner's FINAL POLISH brief lists 11 items. Design rule: **check first, build what's missing, never rewrite what works.** Universal layer (identity/conflayer/mcp/models/webhooks/routing/memory/audit/files/fleet/budget/links/snapshots/activity) is complete + tested — DO NOT TOUCH.

## Verified state (from codebase exploration)

| Item | State | Existing code |
|---|---|---|
| QR | MISSING | `core/lan.py` has only a decorative non-scannable frame; `dashboard --share` CLI flag referenced but unwired |
| npm pack | MISSING | no `package.json`, no `bin/` |
| PATH install | MISSING | `atropos` script exists, shebang present, but no install cmd |
| CLI default action | MISSING | no `cli.default_action` setting |
| Update system | PARTIAL | `core/update.py` atomic git updater exists (no check/apply/auto modes, no dry-run conflict report) |
| AI update engine | MISSING | — |
| Multi-backend sync | MISSING | no `core/sync.py`; only conflayer/identity/skills "sync" (copy, not delta) |
| Multi-backend backup | PARTIAL | `core/backup.py` local tarball only; no S3/server/pair/file backends |
| Setup wizard | EXISTS | `core/setup_wizard.py` — verify against item-9 spec, upgrade gaps |
| Dashboard polish | PARTIAL | 37 panels; scrollbars/icon/empty-state audit needed |
| Moirai narrative | PARTIAL | README + About mention Moirai; TUI/CLI trio-line + tagline + ASCII icon missing |

## Hard rules (non-negotiable)

- Stdlib only — no pip, no non-stdlib imports ever. S3 via urllib+hmac.
- Dashboard single-file `index.html` + `sw.js` + `chat.html`, <400KB total.
- No fabricated output — every claim verified with real commands/tests.
- No secrets in code; secrets never sync (secrets.json excluded by default).
- Farsi UTF-8 + RTL preserved. Comments English, minimal, human — no AI-flavored noise.
- 417 existing tests + new suites all green. No regressions.
- Routers stay nain/omni/local. Product stays Atropos. Moirai = branding only.
- Every feature reachable from CLI + dashboard (+ TUI where sensible).

## New architecture

New leaf modules (independent, build in parallel):
```
core/qr.py          QR v1-4 byte-mode ECC-M encoder + RS/GF256 + masking + PNG/SVG/ASCII
core/sync.py        multi-backend delta sync (gh/server/pair/file)
core/update_ai.py   AI update engine (diagnose→rewrite→test→confirm→apply; mocked LLM)
core/s3.py          minimal S3 SigV4 PUT/GET via urllib+hmac
core/pairing.py     (shared by sync+backup) 6-digit code + expire
```
Extended: `backup.py` (5 backends + retention keep-N/weekly-M + restore preview), `update.py` (check/apply/auto modes + dry-run conflict report), `setup_wizard.py` (verify/upgrade).

Shared files wired serially: `atropos` CLI, `core/settings.py`, `core/dashboard.py` routes (`/api/qr,/api/sync,/api/backup,/api/update,/api/update-ai,/api/wizard/status`), `dashboard/index.html` panels.

New tests: `test_qr.py`, `test_sync.py`, `test_update_ai.py`, `test_backup_multi.py`, `test_s3.py`, `test_wizard.py` + verification exercises.

## Data/state

- `~/.atropos/sync/` — flat JSON/YAML store, `sync/.conflicts/` (LWW loser backup), `sync/.versions/` (per-file history), `sync/peers.json`.
- `~/.atropos/backups/` — existing, extended with backend records.
- `~/.atropos/update_state.json` — existing, extended with check/apply/rollback records.
- `~/.atropos/update_ai.json` — AI engine history per attempt.
- Settings additions: `cli.default_action`, `update.auto` (off|check|apply), `update.channel` (stable|beta), `update-ai.model/effort/mode`, `sync.interval`, `sync.server.url/token`, `backup.retention_weekly`, `backup.s3.*`.

## Verification gate (must show real output)

`python3 -m py_compile core/*.py atropos; python3 -m unittest discover tests` (417 + new green); `python3 atropos version`; `cd / && ~/.local/bin/atropos version`; `python3 atropos dashboard --share` (scannable QR); `atropos sync status`; `atropos backup list --all`; `atropos update check`; curl `/api/qr`, `/api/sync`, `/api/backup`, `/api/update`, `/api/update-ai`, `/api/wizard/status`; dashboard panels render.

## Deliverables

- VERSION stays 1.4.0. README (new features + PATH install + npm publish steps), docs/CHANGELOG.md (final-polish entry), AGENTS.md, architecture.md touched only where relevant.
- Commit + push main. Per-item report table: item → status → evidence (file + test + command output) + deviations.
