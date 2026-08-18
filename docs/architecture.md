# Atropos Architecture — Hermes + Claude Code Fusion

## The problem
Every redeploy wipes `/opt/hermes-agent` (ephemeral filesystem). The live
adapter, installed PTB, any code changes — all gone. Hermes' built-in session
memory and cron survive (persistent volume at `/data`), but code patches vanish.

Meanwhile, Claude Code gives surgical coding skill that Hermes alone lacks
(latency-bound model, no multi-file awareness). But Claude's edits also don't
survive redeploys — they land on the ephemeral filesystem and get wiped.

## The solution: Atropos
Atropos is not a third thing — it is the **wiring** that makes Hermes and
Claude Code function as one organism across redeploy cycles.

```
┌──────────────────────────────────────────────────────────┐
│                    THE ATROPOS LOOP                       │
│                                                          │
│   ┌─────────────────────┐    ┌────────────────────────┐  │
│   │   Hermes Agent      │    │    Claude Code         │  │
│   │                     │    │                        │  │
│   │  • orchestration    │◄───│  • code writing        │  │
│   │  • memory/sessions  │    │  • debugging           │  │
│   │  • cron/scheduler   │    │  • analysis            │  │
│   │  • comms (Telegram) │    │  • multi-file edits    │  │
│   │  • patch dispatch   │    │                        │  │
│   └────────┬────────────┘    └──────────┬─────────────┘  │
│            │                            │                 │
│            ▼                            ▼                 │
│   ┌──────────────────────────────────────────────────┐   │
│   │              THE PERSISTENT LAYER                │   │
│   │                                                  │   │
│   │  /data/.hermes/          /data/workspace/         │   │
│   │  • state.db              • apply_guest_patch.py   │   │
│   │  • config.yaml           • guest_notify.py        │   │
│   │  • SOUL.md               • tests                  │   │
│   │  • memories/             • atropos/ (this repo)   │   │
│   │  • scripts/                                        │   │
│   │  • skills/                                         │   │
│   └──────────────────────┬───────────────────────────┘   │
│                          │                                │
│                          ▼                                │
│              ┌─────────────────────┐                      │
│              │  GitHub: arophin/   │                      │
│              │  • Hermesbp (backup)│                      │
│              │  • Atropos (source) │                      │
│              └─────────────────────┘                      │
└──────────────────────────────────────────────────────────┘
```

## Weakness coverage

| Weakness | Who covers it | How |
|---|---|---|
| Hermes can't write complex code | Claude Code | `claude-atra.sh -p <brief> --permission-mode acceptEdits` in background |
| Claude Code edits die on redeploy | Hermes patch script | `apply_guest_patch.py` re-applies everything from scratch (self-healing) |
| Claude Code can't run comms/Telegram | Hermes | All platform code stays in Hermes; Claude never touches adapter.py directly |
| Router latency (~100–175s/turn) | Claude Code background | Long tasks run `background=true` + `notify_on_complete=true` |
| PTB version oscillates 22.6↔22.8 | Patch script step 0 | `ensure_ptb_version()` subprocess probe + pip install, every run |
| Timezone resets to UTC on deploy | Patch script step 7 | `ensure_tehran_timezone()` ln -sf every run |
| Guest mode breaks (PTB missing GUEST_MESSAGE) | Patch script | `getattr(filters.UpdateType, 'GUEST_MESSAGE', None)` guard, safe on both versions |
| Unauthorized messages invisible | Patch script P9 + guest_notify.py | Logs them to ATRA log channel, never raises |
| Disk fills up | atropos_update.sh pre-clean | Cleans npm/pip caches before install; storage_watch.sh guards ≥80% |

## Update flow (auto-update from source)

### Trigger
- `atropos_update.sh` runs on Artan's command (not auto — restarts need approval)
- OR manual: `bash /data/.hermes/scripts/atropos_update.sh`

### Sequence
1. **Fetch upstream**: `git -C /opt/hermes-agent fetch origin`
2. **Dry-run diff**: `git diff HEAD..origin/main --stat` — show what changed
3. **Artan approves**: prompt in Telegram ("updates available, apply?")
4. **Reset**: `git -C /opt/hermes-agent reset --hard origin/main`
5. **Re-patch**: `python3 /data/workspace/apply_guest_patch.py` (self-healing, all patches from scratch)
6. **Doctor**: `bash atropos_doctor.sh` — verify PTB, patches, timezone, disk, claude
7. **Notify**: report what changed, whether patches passed, disk after

### Safety rails
- Never auto-restart the gateway — always ask Artan
- `atropos_doctor.sh` runs AFTER patches — catches breakage before restart
- If patches fail, the old adapter is gone (reset already ran) — doctor aborts with `DOCTOR FAILED` and Artan must intervene manually

## Doctor checks
`atropos_doctor.sh` verifies:
1. PTB version >= 22.8
2. Adapter exists and AST parses
3. All 11 patches present (guest handler, TypeHandler import, register, send suppress, guest_identity, reaction bridge, processing reactions, P8 DM guard, P9 guest notify)
4. `guest_notify.py` exists at `/data/.hermes/scripts/`
5. Timezone = Asia/Tehran (+03:30)
6. Disk < 80% on /data
7. Claude binary available (`claude --version`)
8. `apply_guest_patch.py` py_compile OK
9. `guest_persona.md` exists
10. `.gh_backup_token` exists and repo reachable

## Patch inventory (as of 2026-08-14)
| # | Name | Target | What it does |
|---|---|---|---|
| 1 | import TypeHandler | adapter imports | Adds TypeHandler to imports |
| 2 | _effective_update_message extra | _effective_update_message | Hides guest messages from normal handlers |
| 2b | send suppress | send() method | Prevents guest replies leaking to Artan's DM |
| 3 | guest handler block | After _handle_text_message | Full _handle_guest_message method + persona + identity |
| 4 | register main | Handler registration | Registers GUEST_MESSAGE handler + excludes from text |
| 5 | register rebuild | Reconnect path | Same as 4 for polling reconnect |
| P6 | reaction bridge | After _clear_reactions | add_reaction / remove_reaction methods |
| P7 | processing start reaction | on_processing_start | 🔥 instead of 👀 while thinking |
| P7b | processing done success | on_processing_done | ✅ instead of 👍 on success |
| P8 | DM chat/user mismatch guard | _handle_text_message auth | Drops mismatched DMs (guest→owner leak) |
| P9 | guest notify on unauthorized | _handle_text_message auth | Logs rejected messages to ATRA log channel |
| — | persona self-heal | Post-patch | Recreates guest_persona.md if missing |
| — | timezone self-heal | Post-patch | Sets /etc/localtime to Asia/Tehran |

---

## v1.4.0 — universal resources, routing & mobile chat (2026-08-15)

The v1.2 settings hub became the seed of a universal-resource layer:

- **Three deployment modes (the Core Law)** — `shared` / `per-harness` /
  `atropos-only` with hash-guard conflict resolution (overwrite/keep/diff)
  applied to identity files (`core/identity.py`), config mirrors
  (`core/conflayer.py`), MCP (`core/mcp.py`), models (`core/models.py`),
  webhooks (`core/webhooks.py`), commands (`core/commands.py`).
- **Routing hub (`core/routing.py`)** — category → clotho/lachesis/atropos/
  auto with stdlib keyword heuristics; settings `routing.map`.
- **Chat engine (`core/chat.py`)** — own sqlite store `~/.atropos/chat.db`
  (hermes state.db untouched), LLM transport via the active router,
  slash commands through the Console whitelist, SSE stream
  (`POST /api/chat/stream`), one-shot share links (`core/links.py`).
- **Ops modules** — fleet, budget gate, snapshots, activity timeline,
  files (read-only), audit matrix, announce feed, LAN sharing + device
  pairing (`core/lan.py`).
- **Surfaces** — dashboard grew to 43 panels (Filters/Agents/Telegram
  added in 1.4.1, i18n 11 langs, 9 themes, bottom-nav mobile),
  `dashboard/chat.html` mobile page with action sheets + stop + pin/rename,
  TUI +8 panels, CLI 69 commands, 861 tests green (1.4.2-beta).
