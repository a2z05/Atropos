# Atropos feature parity — capability | Hermes | Claude | Atropos | status

Living matrix. Every row checked off or marked TODO. Built from the live
scan: Hermes 114 tool files, Claude 45 CLI flags, Atropos cmd table.

## From Hermes

| Capability | Hermes | Atropos | Status |
|---|---|---|---|
| TTS / voice | `text_to_speech` (edge/openai/elevenlabs/gemini/piper/neutts) | `core/tools.tts` via 9Router `/v1/tts`, `atropos tts` | ✅ |
| Vision | `vision_analyze` | `core/tools.vision` via gateway, `atropos vision` | ✅ |
| Image generation | `image_generation_tool` | `atropos imagine` (gateway `/v1/images/generations`) | ✅ |
| Video generation | `video_generation_tool` (flux3/xai) | `atropos video` (gateway `/v1/videos/generations`) | ✅ |
| Delegation / subagents | `delegate_task` | `core/agents.py` + `atropos delegate`, `atropos agent run` | ✅ |
| Session search (content) | FTS5 session search | `chat.search_messages` + `atropos search <q>` | ✅ |
| Cron jobs | cron/*.yaml + no_agent | `atropos cron list` (read-only, mirrors hermes files) | ✅ (list) |
| Email (himalaya) | IMAP/SMTP | `atropos email inbox/send` (needs himalaya) | ✅ |
| Kanban | task board | `core/tools.kanban_*` + `atropos kanban` | ✅ |
| Web tools | `web_search`, `web_fetch`, url safety | `atropos web search/fetch` via 9Router `/v1/search`+`/v1/web/fetch` | ✅ |
| X/Twitter (xurl) | post/search/DM | `atropos x post` (needs xurl CLI) | ✅ |
| YouTube | transcripts → summaries | `atropos youtube <url>` (yt-dlp) | ✅ |
| Office docs | docx/xlsx/pdf/pptx create-edit | `atropos docs <kind> <path>` (view manifest; edit needs hermes stack) | ◐ |
| Spotify | playlist automation (OAuth) | not built (OAuth — out of scope this round) | TODO |
| Smart home (openhue) | lights control | `atropos hue` (needs openhue CLI) | ✅ |
| Memory | persistent memory | `core/memory.py` (universal, private-tag aware) | ✅ |
| Hook system / event hooks | inbound-activity hooks | `core/middleware.py` (before/after model & tool, on_start/on_end/on_error) + universal webhooks | ✅ |
| Guest mode | guest persona | `core/guest.py` isolation (same engine, filtered context, guard rails) | ✅ |
| Approve gate / write approval | approval flows | middleware `approval` filter + universal ask-first | ✅ |

## From Claude Code

| Capability | Claude | Atropos | Status |
|---|---|---|---|
| All CLI flags | --resume/--continue/--restore/--fork-session/--session-id/--print/--output-format/--model/--temperature/--permission-mode/--yolo/--max-turns/--max-budget/... | CLI superset round: `--lang/--theme` + menu/REPL + prompts/tables; flag parity partially mapped to argparse (documented in §4) | ◐ |
| Subagents | `Subagent` tool | `core/agents.py` (defs, harness resolve, background) + `atropos agent` | ✅ |
| Hooks | PreToolUse/PostToolUse/Notification/Stop | `core/middleware.py` hooks (+ custom_filters/ YAML+py) | ✅ |
| MCP | servers + `--mcp-config`/`--strict-mcp-config` | universal `core/mcp.py` registry | ✅ |
| Custom agents | `--agents` JSON | `~/.atropos/agents/*.json` + `atropos agent` | ✅ |
| Slash commands | custom slash commands | built-in slash in REPL/Telegram + custom `custom_filters/` | ✅ |
| Skills | universal skills | `core/skills.py` + marketplace | ✅ |
| Auto-compact | `--autocompact` | middleware `summary` filter (compacts >60k prompts) | ✅ |
| Output formats | text/json/stream-json | `--json` on list commands + `_print_op` JSON | ◐ (text+json) |
| Safe mode | `--safe-mode` | agents `permissions: read-only` + console whitelist | ✅ |
| Background agents | `--bg`, `claude agents` | `atropos agent start` (background), results persisted | ✅ |
| Prompt suggestions | `--prompt-suggestions` | chat empty-state suggestions (chat page) | ✅ |
| Debug file | `--debug-file` | `--debug` on request; logs to ~/.atropos | ◐ |
| Session replay | `--replay-user-messages` | `atropos chat sessions/export` (messages persisted per session) | ◐ |
| Fallback model | `--fallback-model` | middleware `retry` fallback router + failover chain | ✅ |
| Remote control / tmux / chrome | newer modes | `core/tools.bridge` (RAFT wake/activity) + `atropos bridge` | ◐ |

## Atropos-only

| Capability | Atropos |
|---|---|
| Self-healing patch engine | 12 code transforms, rollback-safe, idempotent |
| Multi-backend sync/backup | file/s3/server/github/pair |
| 3-mode Core Law | shared / per-harness / atropos-only |
| Filters & Plugins | 18 prebuilt human-named middleware |
| Moirai lore layer | daily oracle line, doctor verdicts, session names |
| 43-panel dashboard | desktop + mobile-complete bottom nav |

Legend: ✅ built & tested · ◐ partial/shim · TODO not started.