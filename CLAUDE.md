# CLAUDE.md — Atropos project guide

Atropos — self-healing control plane fusing Hermes + Claude Code. Pure-stdlib Python 3.14, no pip. See `AGENTS.md` (agent identity), `docs/architecture.md`, `docs/PARITY.md`.

## Status (2026-08-18)
- v1.4.1-beta shipped (round 2). Commit `aa55254`.
- **v18 complete, 1.4.2-beta shipped** — head `7bcebec` (pushed, repo moved to `github.com/a2z05/Atropos`): phases 1+2 (A ports, B, E, H, cron, F, G, I, J+K, L), approve port, release #43. **861 tests green.**
- Remaining: G tail (rich `telegram.*` settings, per-chat ops confirm), Skills panel, chat rebuild depth — see `docs/FUTURE.md`.

## Key Decisions (v18)
| Decision | Why |
|---|---|
| Copy > reinvent; every port cites Hermes source path in docstring | Brief A.1 owner-ordered; deviations documented |
| Gateway stays first provider in tts/vision/imagine chains | tools.py shims + existing tests stay green |
| Railway features gated on `detect_cloud()=="railway"`; outbound-only network (relay, no inbound ports) | v18 B + H2 (Railway blocks SSH/inbound) |
| Scissors ✂ ≤3 repo-wide — only `core/ascii.py` banner/shears; chat/telegram use ✦ | v18 D |
| Agent ports use worktree isolation | concurrent ports must not clobber shared files |
| `{**(payload or {})}` on dashboard POSTs | empty body → TypeError otherwise |
| Approval gate = middleware filter (`before_tool`), fail-closed headless, hardline never bypassable | v18 A.11; Hermes parity with settings-backed config |
| `approvals.mode: off` still blocks hardline + deny rules | Hermes ordering: floor before bypass |
| Middleware approval rejects via detection, not `check_all_command_guards` (non-interactive auto-approves) | a headless filter has no human to ask — fail closed |
| 77 DANGEROUS + 12 HARDLINE patterns byte-identical to Hermes (AST-verified in tests) | mechanical diffability, parity discipline |
| Migrate backup timestamps use `-` not `:` | Windows dir-name constraint |
| Sealed guest memory: owner view = counts only, by construction | know, cannot tell |

## New Files / Structure (phase 1 + 2)
```
core/search.py      FTS5 search (hermes_state_search.py)     core/railway.py   Railway status/volume/deploy/health
core/web.py         web tools (web_tools.py)                 core/fate.py      daily fate, weave counter, easter eggs
core/x.py           X via xurl (x_search_tool.py)            core/sync_live.py live sync + relay + journal
core/ha.py          Home Assistant (homeassistant_tool.py)   core/autoskill.py F: usage/lifecycle/curator/attribution/orchestrate
core/tts.py         TTS 7-provider chain (tts_tool.py)       core/documents.py docx/xlsx/pptx/PDF (read_extract.py)
core/vision.py      vision analyze (vision_tools.py)         core/imagine.py   FAL + gateway (image_generation_tool.py)
core/cron.py        cron list (hermes cron)                  core/approve.py   dangerous-command gate (approval.py, 77+12 patterns)
core/kanban.py      task board (hermes kanban)               core/safety.py    write-approval + file_state
core/skills.py      skill machinery (I)                      core/migrate.py   ask-first import (J)
core/guest.py       sealed memory (K)                        install.sh        one-liner curl|sh installer
```
Tests: `test_copy_parity.py` (78: docs/safety/kanban/approve hermetic parity), `test_middleware.py` (17, +ApprovalGateTests), `test_creativity.py` (12), `test_railway.py` (11), `test_sync_live.py` (9), `test_tts_vision_imagine.py` (28), `test_skills.py` (24), `test_migrate.py` (16), `test_dashboard_redesign.py` (10). Logs: `CC-Session-Logs/`.

## Key Patterns / Insights
- `detect.atropos_home()` reads **ATROPOS_HOME env first** — test home-swaps must set env, not patch `detect._home`.
- Hermes FTS5 = external-content virtual table + triggers; short-CJK → LIKE fallback; sanitizer quotes dotted/hyphenated terms.
- `_resolve_conflict(rel, hash, bytes, ...)` wants sha1 hexdigests, not raw bytes.
- Backup manifest rides in the tar as `_MemFile` (TarInfo + BytesIO); `_secret_name` filter drops token files.
- Module-global queues (`sync_live._QUEUE`) must be reset per-test — suite-order leakage otherwise.
- Background agents hit 429 rate limits → resume via SendMessage; keep port batches small.
- Hermes source: `C:\Users\a2z\AppData\Local\hermes\hermes-agent\tools` (111 files). HERMES_HOME = `%LOCALAPPDATA%\hermes` on this box.
- Owner handle: `a2z05` / repo `github.com/a2z05/Atropos` — never the owner's real name in code/tests/docs.

## Blockers / Warnings
- 429 FreeUsageLimitError on background agents — retry with delay or resume.
- deepmo classifier outages block Bash until retry — do read-only work meanwhile.
- GitHub repo moved: `arophin/Atropos` → `a2z05/Atropos` (push auto-redirects).
- Windows cp1252 stdout: never print non-ASCII in CLI banners (approve.py uses `[approval]`, not ⚠️).
- Don't touch: `core/dashboard.py`, `core/telegram.py`, `core/tools.py` in agent ports (coordinator owns them).

## Next Steps
1. **G tail**: rich `telegram.*` settings + per-chat `ops_allowed` two-step confirm in the bot.
2. **Skills panel** in the dashboard (lint badges, environments, provenance).
3. `test_dashboard_redesign.py` depth: quick actions invoke endpoints, not just markup.
4. Chat rebuild parity pass (dashboard chat vs chat.html).
5. Approval UX: Telegram notify callback for the gateway approval queue.
6. More Hermes ports (video/youtube/hue/audio real implementations). See `docs/FUTURE.md`.
