# Session Log: 17-08-2026 - v18-addendum-porting-round

## Quick Reference (for AI scanning)
**Confidence keywords:** v18 addendum, Hermes porting, stdlib-only, FTS5, web_tools, tts_tool, vision_tools, railway, sync_live, fate, autoskill, migrate, guest, bot-ops, ai-mod, install.sh, dashboard rebuild, scissors restriction, 723 tests
**Projects:** Atropos (C:\Users\a2z\Documents\atropos)
**Outcome:** v18 phase 1 committed + pushed (f1d0ad2): section A source-copy ports (search/web/x/ha/tts/vision/imagine/documents), railway (B), fate (E), live sync + complete backup (H). 12-agent workflow running for remaining section-A modules + CLI polish (C) + migration/guest (J/K). Suite green at 723.

## Decisions Made
- **Copy > reinvent** for section A: every port cites its Hermes source path in the docstring; deviations documented + why (aiohttp→urllib/subprocess CLI, oga piper package→piper CLI binary, no outpaint upstream→gateway passthrough).
- **Gateway stays first provider** in tts/vision/imagine chains so existing tools.py shims + tests stay green.
- **Railway features gated** on `detect_cloud()=="railway"`; network posture outbound-only (H2: relay mode, no inbound ports).
- **Scissors restriction**: repo ✂ ≤3 — only in core/ascii.py banner/shears glyphs; chat/telegram switched to ✦ thread mark; dashboard cut animation uses thread mark.
- **detect.atropos_home() checks ATROPOS_HOME env first** — test home-swaps must use env, not detect._home patching.
- **Worktree isolation** for the 12-agent workflow so concurrent ports don't clobber shared files (tests/test_copy_parity.py append-only).

## Key Learnings
- Hermes state search uses FTS5 external-content tables with triggers + 3 indexes (unicode61/trigram/cjk-bigram); short-CJK falls back to LIKE. Ported the sanitizer (quoted phrases, dotted-term quoting) + anchored window/bookends.
- `{**payload or {}}` guard needed for dashboard POSTs with empty bodies.
- Test-order state leakage: module-global `_QUEUE`/`_LAST` in sync_live must be reset per test.
- The auto-classifier can rate-limit (429 FreeUsageLimitError) — keep batches small, use worktrees, resume via SendMessage.

## Solutions & Fixes
- FTS5 upgrade of chat.search_messages (LIKE → FTS5 + fallback), verified by 9 parity fixture tests.
- `_resolve_conflict` needs hash strings not bytes — sha1 hexdigest.
- Backup manifest: `_MemFile` tar members via TarInfo + BytesIO; `_secret_name` filter excludes tokens.
- restore(): added `atropos/*` branch to write into ~/.atropos, MANIFEST skipped as metadata.
- sync_live push handler must ALSO append to _QUEUE so polls from third peers work (relay-style bridge).

## Files Modified
- `core/search.py` (new): FTS5 search + anchored view (ported hermes_state_search.py)
- `core/web.py x.py ha.py tts.py vision.py imagine.py documents.py` (new): section A ports
- `core/railway.py` (new): status/volume/deploy/health + doctor extra checks
- `core/fate.py` (new): daily fate, weave counter, cut frames, lore easter eggs
- `core/sync_live.py` (new): serve/push/poll/relay live sync + conflict journal
- `core/backup.py`: complete scope + MANIFEST.json + checksums + token masking + atropos/ restore branch
- `core/dashboard.py`: `/health` endpoint, deploy check at serve()
- `core/chat.py`: FTS5 init in _init_db, search_messages rewrite
- `core/doctor.py`: railway volume/stale-pid check
- `core/settings.py`: skills.auto_skill + skills.auto_memory keys
- `core/autoskill.py` (new): usage telemetry, lifecycle, attribution, curator, orchestrate
- `core/ascii.py`: unchanged (banner scissors allowed)
- `tests/`: test_copy_parity (9 classes), test_creativity (12), test_railway (11), test_sync_live (9), test_tts_vision_imagine (28)
- `atropos` CLI: cmd_railway, cmd_lore --fate
- `languages/en.json + fa.json`: fate_lines + lore_stories keys

## Setup & Config
- HERMES source root: C:\Users\a2z\AppData\Local\hermes\hermes-agent\tools (111 tool files)
- Hermes runtime: C:\Users\a2z\AppData\Local\hermes (HERMES_HOME on Windows)
- Canonical repo: github.com/a2z05/Atropos (owner handle = a2z05, never the owner's name)
- VERSION: 1.4.1-beta → will bump to 1.4.2-beta at release (#43)

## Pending Tasks
- **Sections A-remainder/C/J+K**: 12-agent workflow wf_4ba06f7b-241 running (delegate, cron, kanban, approve, process, safety, browser, smallcluster(daemon/checkpoints/threat/cli), question, comms-blueprints, c-clipolish, jk-migration-guest)
- **F**: autoskill.py written — needs tests/test_autoskill.py + CLI wiring + full-suite run
- **G**: telegram bot ops as tools + rich settings + ai-mod + install.sh + test_bot_ops/test_ai_mod/test_install
- **I**: skill system adoption (Hermes skill_manager, provenance, usage, sync, lint) + Skills panel + code comparison pass
- **L**: dashboard rebuild — Command Center landing + rebuilt chat + full panel redesign + test_dashboard_redesign
- **43**: VERSION → 1.4.2-beta, README/CHANGELOG/AGENTS + docs/PLUGINS.md SDK.md FUSION.md FUTURE.md, full battery, commit+push, per-section report with audit matrix + NEW DISCOVERIES

## Errors & Workarounds
- **429 rate limits** on background agents → use sleep + resume via SendMessage / smaller agent pools.
- **git stash conflict** from media agent mid-port → pop+resolve: pre-existing edits restored (stash popped, stale stash dropped).
- **test home-swap confusion**: patching detect._home ineffective because atropos_home() reads ATROPOS_HOME env first.
- **backup helpers vanished** after an agent's overwrite → re-applied (manifest + helpers).
- **dashboard POST empty body** → TypeError with `{**payload}` → fixed with `{**(payload or {})}`.

## Key Exchanges
- Media cluster (tts/vision/imagine) ported by a background agent then recovered from a 429 + stash incident — 28 tests.
- User: "go ultracode with many agents" → 12-agent workflow with worktree isolation + adversarial verify phase.
- User: "use 12 agents at once" → workflow relaunch.
- /compress run: preserving session for the v18 round.

## Custom Notes
None

---

## Quick Resume Context
Atropos v18 addendum in progress: phase 1 (sections A-partial/B/E/H) committed as f1d0ad2 and pushed (723 tests green). A 12-agent background workflow (wf_4ba06f7b-241) is porting the remaining section-A modules + CLI polish + migration/guest with per-agent worktrees; merge its worktree outputs into core/ when it completes, then run the full suite. Remaining solo sections: F (autoskill tests+CLI), G (telegram bot ops + ai-mod + install.sh), I (skills adoption), L (dashboard Command-Center rebuild), then release #43 (VERSION 1.4.2-beta, docs, commit+push, report). Bot ops tool surface is the last big cross-cutting item: core/telegram_bot_ops.py with 20 tools + per-chat ops_allowed + two-step confirm.

---

## Raw Session Log
(truncated — full history in the conversation transcript JSONL per /compress guidance)