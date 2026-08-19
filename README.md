# Atropos — the harness that cannot be turned

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6366f1?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6?style=flat-square)](LICENSE)
[![stdlib only](https://img.shields.io/badge/stdlib%20only-22d3ee?style=flat-square)](#rules)
[![889 tests](https://img.shields.io/badge/tests-889%20green-34d399?style=flat-square)](#tests)
[![PRs welcome](https://img.shields.io/badge/PRs%20welcome-34d399?style=flat-square)](https://github.com/arophin/Atropos)

> **Atropos** (Ἄτροπος) — the Greek Fate who cuts the thread at the appointed moment: *she who cannot be turned.*

Atropos is a **self-healing control plane** for AI agents running in ephemeral cloud environments. It fuses **Hermes Agent** (orchestration, comms, memory, persistence) with **Claude Code** (coding, debugging, surgery) — one brain, two hands, both kept alive by a single deterministic, **stdlib-only** harness. No pip, no lockfile, no node_modules in Python land: `unittest`, `http.server`, `sqlite3`, `urllib`, `shutil` — that's the whole stack.

---

## ✨ What Atropos is

```
                            ┌──────────────────────────────────────────┐
                            │            Atropos CLI (62 cmds)         │
                            │  doctor · route · patch · update · …     │
                            │  settings · skills · plugin · failover   │
                            └──────┬───────────────────┬───────────────┘
                                   │                   │
                ┌──────────────────▼────────────────────▼──────────┐
                │                core/ (stdlib only)               │
                │  settings  config  detect  doctor  extensions    │
                │  marketplace console failover sse                │
                │  patches  router  update  guest  backup  skills  │
                │  watch  logs  alerts  jailbreak  tui  wizard     │
                └───────┬───────────────────────────────▲──────────┘
                        │                               │
                 git fetch │            ┌───────────────┘
                 (upstream)│            │ dashboard/ (1 file + sw.js)
                        ▼               │  43 panels · 90+ APIs · SSE
        ┌───────────────────────────────┴──────────────────────────┐
        │  hermes-agent (git repo)      dashboard :8787            │
        │  adapter.py ← 12 hacks        11 langs · 9 themes · PWA  │
        └──────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick start

```bash
git clone https://github.com/arophin/Atropos.git
cd Atropos

python3 -m py_compile core/*.py atropos   # syntax check
python3 atropos setup                     # first-time wizard
python3 atropos doctor                    # 7 health checks
python3 atropos route set nain            # pick a router: nain | omni | local
python3 atropos dashboard                 # web control plane on :8787
```

Open `http://127.0.0.1:8787`, paste the token printed by the dashboard (stored at `~/.atropos/auth_token`).

### Install from anywhere (PATH)

```bash
python3 atropos install              # symlinks (or copies) `atropos` into ~/.local/bin
cd / && atropos version             # works from any directory
```

On Windows the install copies a self-contained runtime next to the script
(`core/`, `templates/`, `hacks/`, `patches/`, `dashboard/`, `VERSION`) so the
binary keeps working no matter where you run it. `--bin <dir>` overrides the
target directory.

### npm packaging (owner-only)

`package.json` declares `atropos-hs` with `bin` entries `atropos` and
`atropos-dashboard`. Publishing is **not** part of any CI — the owner pushes
with their own npm token:

```bash
npm version patch                     # bumps package.json + VERSION
npm publish                           # requires owner npm token + repo access
npm i -g atropos-hs                   # consumers: atropos + atropos-dashboard on PATH
```

---

## 🖥 Dashboard — 43 panels, 90+ API endpoints

A single-file HTML/CSS/JS control plane with glassmorphism, particle canvas, live SSE push, **9 themes** (dark / light / black / sepia / midnight / matrix / ink / embers / glass + auto), five accents and **11 languages** (en master + fa/ar/he/ur RTL, de/fr/es/ru/tr/zh/hi/it partial) — zero external dependencies, installable as a PWA.

| Panel | What it shows |
|---|---|
| **Overview** | Runtime, disk, router status, quick stats |
| **Doctor** | 7 health checks, one-shot auto-fix |
| **Patches** | 12 declarative hacks, verify/apply, inline diffs |
| **Routers** | nain/omni/local switching, live ping, models, **latency sparklines**, **auto-failover state** |
| **Sessions** | state.db sessions, trace drill-down, **search**, **export** |
| **Cron** | Cron jobs from hermes `cron/*.yaml` |
| **Skills** | Hermes + Claude skills, universal store |
| **Plugins** | Hermes plugin directory |
| **Update** | Atomic updater, behind-count, diff, changelog viewer, one-click apply |
| **Guest** | Guest mode toggle, persona editor |
| **Logs** | Gateway tail + live streaming |
| **Config** | Atropos + Hermes config editors |
| **Claude** | Binary, settings.json, aliases, `claude doctor` runner |
| **Analytics** | Messages/sessions/cost |
| **History** | Action audit trail (JSONL) |
| **Backup** | History, create now, schedule, retention |
| **Effort** | 7 tiers (minimal → tryhard) per harness |
| **Self-Heal** | Doctor → patches → watch, one click |
| **Alerts** | Telegram alerting, per-event toggles |
| **Jailbreak** | Restriction scanner + 7 bypasses |
| **Market** | 🆕 Trusted registries (Anthropic Skills, Superpowers, hermes plugins) with one-click install |
| **Console** | 🆕 Safe whitelist-only REPL (doctor, backup create, route test, skills install…), SSE output |
| **Settings** | 🆕 Every config key, typed editors, theme/lang/accent, YAML export/import |
| **MCP** | 🆕 Universal registry: rescan harnesses, adopt ask-first, probe, project-to-harness (S/H/A🔒) |
| **Identity** | 🆕 SOUL/AGENTS/SYSTEM/GUEST/CODE_STYLE: edit, mode, diff, version history, restore |
| **Configs** | 🆕 hermes/claude/router/atropos configs: validate, edit, snapshot, rollback, conflicts |
| **Routing** | 🆕 Category -> Clotho/Lachesis/Atropos/auto, custom categories |
| **Files** | 🆕 Read-only repo browser + search + preview |
| **Models** | 🆕 Universal models, per-harness assignment |
| **Webhooks** | 🆕 Event registry, toggle, test |
| **Pairing** | 🆕 LAN share URL + QR frame, device approval |
| **Fleet** | 🆕 Multi-box health grid, ping all |
| **Memory** | 🆕 RAG-lite notes, keyword search |
| **Budget** | 🆕 Token usage, quota gate, auto-failover |
| **Activity** | 🆕 24h timeline (updates/alerts/backups/sessions) |
| **Announce** | 🆕 Tips + changelog + version check, dismissible |
| **Filters** | 🆕 Middleware engine: 18 prebuilt filters (PII, retry, summary, brand…) + custom YAML/py in `~/.atropos/custom_filters/` |
| **Agents** | 🆕 JSON-defined agent workforce, harness auto-resolve, background runs, results history |
| **Telegram** | 🆕 Bot gateway: long-poll, guest modes (allow/read-only/deny), step trails, rotating logs |

### Dashboard features

- **⌘K command palette** — search all 43 panels + actions
- **Mobile-complete** — bottom nav (Overview/Chat/Sessions/Settings/More), modals drop to bottom sheets ≤768px, 44px touch targets, safe-area insets, tables become cards
- **Keyboard shortcuts** — `1..8` jump panels, `⌘K` palette, `?` help
- **Themes & languages** — 9 themes + auto; 11 languages with full RTL for fa/ar/he/ur (`?lang=fa`, the 🌐 button, or `settings.language` / `--lang`)
- **SSE live hub** — one `EventSource` on `/api/events` feeds the Console, Logs and status channels with bounded queues + heartbeat
- **PWA offline** — service worker (`dashboard/sw.js`) + webmanifest: the dashboard loads without a network
- **Auth** — per-browser token + optional password gate; secrets never leave the box unmasked
- **Marketplace** — install 17 official Anthropic skills, 14 Superpowers, hermes plugins — into the right store, no arbitrary URLs

---
## ⚖️ THE CORE LAW — three deployment modes

> *"Everything shared has ONE canonical version in Atropos. When a thing is used through Atropos, both harnesses use Atropos' version. Each item can also be set per-harness — separate, so each harness uses its own. Or it can be defined as Atropos-only: it lives only inside Atropos and is never overwritten by anything."*

Every universal resource (identity files, config files, MCP servers, models, webhooks, commands, secrets refs…) gets three deployment modes:

| Mode | Meaning |
|---|---|
| **shared** (S) | Atropos' copy is canonical; harnesses project from it. Editing in Atropos updates both. A harness file that drifted is a **conflict** — never a silent overwrite (resolve: overwrite / keep / diff). |
| **per-harness** (H) | Each harness keeps its own copy in its own folder; Atropos reads/monitors both, never writes unless asked. |
| **atropos-only** (A+🔒) | Lives ONLY in `~/.atropos/`; never projected, never overwritten by sync/update/import. The owner's personal override layer. |

Override chain: `harness-local → atropos-shared → atropos-only (always wins)`. Discovery is always **ask-first** (MCP `rescan`/`adopt`, identity `detect_new`). Every managed item shows its mode badge (S / H / A+🔒) in the dashboard.

## 🧶 The Three Moirai

The internal brand narrative maps to the real engines — **Clotho** → Hermes Agent (brain: orchestration, sessions, memory, comms) · **Lachesis** → Claude Code (hands: writes/debugs code) · **Atropos** → the system itself (cuts the broken thread and re-weaves it: self-heal, patches, updates). The Moirai names are **user-facing labels only** — technical identifiers (endpoints, config keys, CLI commands, router names `nain/omni/local`, JSON fields, file names) never change. The Overview shows the trio card; chat messages carry the engine badge that answered.

## ↯ Task routing hub

`core/routing.py` decides **which harness does what** — every category (coding, debugging, research, summaries, monitoring…) maps to `clotho | lachesis | atropos | auto`. `auto` uses keyword heuristics (file extensions, "fix/debug/refactor" → Lachesis; "search/summarize/report" → Clotho; system ops → Atropos). Override priority: explicit → auto-heuristic → default. Custom categories are one command away:

```bash
atropos routing list                          # current map
atropos routing set coding lachesis           # explicit override
atropos routing add reviews --harness clotho  # custom category
```

## 📱 Mobile chat + LAN sharing

`atropos dashboard --share` prints the LAN URL (auto-detected IP + port) and a **real scannable QR** (pure-stdlib encoder). The dashboard binds `0.0.0.0` when sharing; new devices need **approval** (Pairing panel), and the optional password gate still applies.

**`/chat`** is a dedicated mobile-first chat page (also great on desktop): glass bubbles with Moirai glyph avatars, **streaming replies** (`POST /api/chat/stream` → SSE deltas), slash commands routed through the Console whitelist (`/doctor`, `/backup`, `/skills list`…), voice input (Web Speech), swipe/pull gestures, offline badge, Farsi RTL, and one-shot **share links** (`atropos links create <session>` → `/chat?share=…`, single-use, 1h TTL, SHA-256-only storage).

Chat round-2: per-message actions (copy / regenerate / edit-and-resend / delete / inspect, via tap-⋯ sheet, 44px targets), fenced code blocks with a copy button, welcome suggestions, a **stop** button that aborts a streaming reply, and full session management — rename, pin-to-top, and delete straight from the drawer (long-press / right-click). Inspect opens the per-message trace (harness, model, effort, latency, tokens, timestamp) — the same data the Sessions trace panel shows.

---

## 🧵 Session Engine — one session for everything

The Single Session Engine routes every conversation (Telegram, dashboard
chat, CLI REPL, agents) through ONE logical entry that decides per message
which session it belongs to. **The reply starts before any deep
classification finishes — always.** Modes:

- **unified** — one session per surface; topics are thread markers inside it. 0 extra ms.
- **auto-split** — a new session per topic, auto-created/resumed by a stdlib keyword classifier (~0.1ms/msg). 0 ms for ~80% of messages.
- **hybrid** — unified base; very-confident new topics split into sub-sessions.

All tunables live in `settings.session_engine.*` (per-surface overrides
via `session_engine.surfaces.*`). CLI: `atropos sessions list|current|threads|route|merge|pin|stats|explain|mode`. Telegram: `/session`, `/thread <name>`. Dashboard: Sessions panel + `POST /api/session_engine/*`. See `docs/SESSION_ENGINE.md`.

---

## 🖥 CLI reference — 70 commands

Bare `atropos` opens the **menu UI** (numbered actions, arrow keys, `/` commands — a trimmed Claude Code-style shell). `atropos repl` is the interactive REPL with `/doctor`, `/backup`, `/lore`…; both are terminal-theme aware. Global flags: `--lang <code>`, `--theme <name>`, `--json`.

| Command | Description |
|---|---|
| `atropos setup [--check]` | First-time wizard: detect → check → install → configure |
| `atropos setup --status` | Discovery summary + which harness owns each group |
| `atropos setup --import <group> [--harness] [--mode]` | Import a resource group (shared/per/monitor) |
| `atropos install [--bin <dir>]` | Symlink/copy `atropos` onto PATH (self-contained on Windows) |
| `atropos update [--check] [--apply] [--ai-check] [--set-auto]` | Update check/dry-run, apply with rollback, AI engine preview, auto mode |
| `atropos sync status / push / pull / host --pair / join <code>` | Delta sync across devices (file/server/github/pair) |
| `atropos backup --backend s3\|server\|github\|pair [--restore]` | Multi-backend backup (file default), restore w/ preview |
| `atropos init` | Detect env, write config, apply patches |
| `atropos version` | Version + runtime |
| `atropos detect` | Full environment detection |
| `atropos status` | System overview |
| `atropos doctor [--fix] [--json]` | 7 health checks + auto-fix |
| `atropos route [set nain\|omni\|local]` | Show or switch the router |
| `atropos failover` | Router failover status/check |
| `atropos patch [--verify\|--apply]` | Hack engine |
| `atropos update [--check]` | Atomic update (rollback-safe) |
| `atropos guest [--status\|--toggle]` | Guest mode |
| `atropos config get/set <key> <value>` | Legacy raw config access |
| `atropos settings` | 🆕 **Settings hub**: table / `get <key>` / `set <key> <value>` / `export` / `import <file>` — typed + validated |
| `atropos logs [--tail N]` | Tail gateway logs |
| `atropos dashboard [--port N]` | Web control plane |
| `atropos watch [--daemon] [--interval N]` | Self-healing watchdog (+ auto-backup, + failover) |
| `atropos backup [--list\|--restore\|--prune N]` | Full backup + rotation (`backup.retention`) |
| `atropos skills [--list\|--sync\|--export\|--import]` | Universal skills |
| `atropos skills --enable/--disable/--remove <name>` | 🆕 Skill lifecycle (reversible) |
| `atropos plugin [list\|enable\|disable\|remove <name>]` | 🆕 Plugin lifecycle |
| `atropos effort [set <tier>] [--hermes\|--claude\|--atropos]` | Per-harness effort tiers |
| `atropos tui` | Arrow-key terminal UI with history |
| `atropos alert [--test\|--check\|--send]` | Telegram alerts |
| `atropos jailbreak [--status\|--apply-all]` | Restriction scanner + bypass |
| `atropos routing list|set <cat> <h>|add <cat> <h>` | Task routing hub |
| `atropos mcp list|add|remove|enable|disable|rescan|adopt` | Universal MCP registry |
| `atropos models list|add|assign <harness> <name>` | Universal models |
| `atropos webhooks list|add|remove|toggle|test` | Webhook registry |
| `atropos identity list|edit|mode|sync|diff|restore` | Identity files (SOUL/AGENTS/...) |
| `atropos configs list|show|edit|validate|mode|rollback|sync` | Universal config manager |
| `atropos audit` | Complete-picture resource matrix |
| `atropos fleet list|add|remove|ping` | Multi-box fleet |
| `atropos budget [--check]` | Token usage + quota gate |
| `atropos links create|list|revoke` | One-shot share links |
| `atropos snapshots list|create|restore` | Snapshot gallery |
| `atropos activity` | 24h timeline |
| `atropos memory add|search|list|stats` | RAG notes |
| `atropos files list|read|search` | Read-only repo browser |
| `atropos chat sessions|send|export` | Chat engine (mobile) |
| `atropos commands list|add|alias` | Commands & aliases |
| `atropos announce` | Announcement feed |
| `atropos dashboard --share` | LAN sharing + QR |
| `atropos repl` | 🆕 Interactive REPL (`/doctor`, `/backup`, `?` for help) |
| `atropos lore` | 🆕 Daily oracle line from the Moirai |
| `atropos middleware list\|enable\|disable\|order` | 🆕 Filters & plugins (18 prebuilt) |
| `atropos agent list\|run\|start\|defs` | 🆕 Agent workforce (JSON defs, harness auto-resolve) |
| `atropos telegram [run\|status]` | 🆕 Telegram gateway (long-poll, guest modes) |
| `atropos search <q>` | 🆕 Session content search |
| `atropos cron` | 🆕 List cron jobs |
| `atropos web search\|fetch` | 🆕 Web tools via 9Router |
| `atropos kanban` | 🆕 Task board |
| `atropos email inbox\|send` | 🆕 Email via himalaya |
| `atropos tts / vision / imagine / video` | 🆕 Media generation via gateway (9Router) |
| `atropos youtube <url>` | 🆕 Transcript → summary via yt-dlp |
| `atropos x post` | 🆕 Post to X via xurl |
| `atropos docs <kind> <path>` | 🆕 Office docs (docx/xlsx/pdf/pptx) |
| `atropos hue` | 🆕 Smart-home lights via openhue |
| `atropos audio <file>` | 🆕 Audio info/analysis |
| `atropos delegate <text>` | 🆕 Delegate to an agent |
| `atropos bridge start` | 🆕 RAFT bridge: /health /activity /wake |
| `atropos migrate plan\|apply\|undo` | 🆕 Ask-first, revertible Hermes state import |
| `atropos approve check\|mode\|allow\|deny` | 🆕 Dangerous-command gate (Hermes approval.py port) |
| `atropos ai-mod status\|preview\|apply` | 🆕 AI self-modification (patch rewrites) |
| `atropos autoskill / curator / attribution` | 🆕 Auto-improve lifecycle (usage, curation, audit) |
| `atropos sessions list\|current\|threads\|route\|merge\|pin\|stats\|explain\|mode` | 🆕 **Session Engine** (v19): one session for everything — unified/auto-split/hybrid |

---

## ⚙ Settings schema (single source of truth)

`core/settings.py` — one typed schema for every key. `atropos settings` prints it:

```
[core]          router.active (nain|omni|local) · router.model · router.base_url
                effort.hermes/claude/atropos · effort_default · update.channel
[watch]         watch.interval · watch.threshold_disk · watch.auto_backup
                watch.state_db_mb · watch.log_max_mb
[alerts]        alerts.enabled · alerts.token ⚫ · alerts.chat_id · alerts.threshold_disk
                alerts.min_interval · alerts.events.{disk,doctor,router,patches}
[dashboard]     dashboard.port · dashboard.host · dashboard.password ⚫
                dashboard.refresh_ms · dashboard.theme · dashboard.lang · dashboard.accent
                dashboard.particles · dashboard.live
[backup]        backup.period (daily|off) · backup.retention
[guest]         guest.enabled · guest.persona_path
[skills]        skills.routing · skills.auto_sync
[jailbreak]     jailbreak.auto_apply
[failover]      failover.enabled · failover.order · failover.retries · failover.hold_minutes
[extensions]    extensions.enabled
```

```bash
atropos settings set dashboard.port 8788      # typed — int
atropos settings set dashboard.port abc       # ERR rejected: expected an integer
atropos settings export > settings.yaml       # secrets masked
atropos settings import settings.yaml         # validated
```

Secret values (`alerts.token`, `dashboard.password`) are masked in the CLI, the API, the export and the audit log — they never appear in `history.jsonl`.

---

## 🛡 Router auto-failover

When the active router fails `failover.retries` times in a row, Atropos switches down the chain **nain → omni → local**, persists every switch to `~/.atropos/failover_state.json`, and raises a Telegram alert. When all three are down it enters a terminal `all_down` state (no wrap-around re-ping loop). A manual `atropos route set` holds the choice for `failover.hold_minutes` — you stay in control, it doesn't fight you.

---

## 🩺 Doctor checks (7)

Python ≥ 3.10 · hermes-agent present · PTB ≥ 22.8 · Claude Code · 12 patches applied · disk < 85% · timezone Asia/Tehran — with `--fix` where possible.

---

## 🗂 Effort tiers (7 levels, per harness)

```
minimal → low → medium → high → xhigh → ultracode → tryhard
```

```bash
atropos effort set tryhard                    # all three harnesses
atropos effort set tryhard --hermes           # hermes only
atropos effort set ultracode --claude         # claude only
```

---

## 🧪 Tests

```bash
python3 run_tests.py              # or: python3 -m unittest discover tests
node tests/test_js_syntax.js      # dashboard JS syntax + panel consistency
```

**861 tests, all green** — YAML parser, config roundtrip, settings schema/coercion/migration/secrets, env detection, doctor, 12-hack engine, router + failover logic, extensions, console whitelist (including `rm -rf /` rejection), live HTTP API with auth, SSE, plus round-2 suites: ASCII identity, i18n (11 langs), CLI menu/REPL/tables, middleware filters (incl. the Hermes approval gate), agents, capability probe, Telegram gateway, parity matrix (77 dangerous + 12 hardline patterns verbatim), guest isolation, dashboard control checks, mobile-complete markup, sealed guest memory, migration import. Pure `unittest`, zero dependencies.

---

## ⚡ Patch engine

12 YAML-defined hacks, topologically ordered, applied to a git-pristine `adapter.py`, verified with greps and `ast.parse`, rollback-safe. Idempotent by construction — the immune system that re-arms after every redeploy.

---

## Why the name?

In Greek mythology, Atropos (Ἄτροπος) was the eldest of the three Moirai — the one who cut the thread of life at its appointed moment. *She who cannot be turned.* Here, she is the one who re-spins the thread: the self-healing harness that keeps the agent alive when everything else tries to shut it down.

---

## Rules

- **stdlib only** — `unittest`, `http.server`, `sqlite3`, `urllib`, `subprocess`, `json`, `shutil`. No pip, no lockfile, not even in CI.
- **No hardcoded `/data` or `/opt` in `core/`** — `detect.py` owns every path via env vars.
- **WHY-only comments in English** — no narration, no TODO/FIXME, no AI-isms; فارسی UI strings correct UTF-8 + RTL-aware.
- **Routers = nain · omni · local** — `deepmo` is the model nain serves, never a router.
- **BETA badge** — version-flagged builds show it; `settings.beta_badge` off hides it.
- **Console /api/run is whitelist-only** — arbitrary shell is forbidden by design.
- **Dry-run only** — Atropos is a control plane, never trades real money.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

*Built to survive redeploys, upstream updates, and its own patches — that's the whole point.*