# Atropos — the harness that cannot be turned

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6366f1?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6?style=flat-square)](LICENSE)
[![stdlib only](https://img.shields.io/badge/stdlib%20only-22d3ee?style=flat-square)](#rules)
[![106 tests](https://img.shields.io/badge/tests-106%20green-34d399?style=flat-square)](#tests)
[![PRs welcome](https://img.shields.io/badge/PRs%20welcome-34d399?style=flat-square)](https://github.com/arophin/Atropos)

> **Atropos** (Ἄτροπος) — the Greek Fate who cuts the thread at the appointed moment: *she who cannot be turned.*

Atropos is a **self-healing control plane** for AI agents running in ephemeral cloud environments. It fuses **Hermes Agent** (orchestration, comms, memory, persistence) with **Claude Code** (coding, debugging, surgery) — one brain, two hands, both kept alive by a single deterministic, **stdlib-only** harness. No pip, no lockfile, no node_modules in Python land: `unittest`, `http.server`, `sqlite3`, `urllib`, `shutil` — that's the whole stack.

---

## ✨ What Atropos is

```
                            ┌──────────────────────────────────────────┐
                            │            Atropos CLI (27 cmds)         │
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
                        ▼               │  23 panels · 60+ APIs · SSE
        ┌───────────────────────────────┴──────────────────────────┐
        │  hermes-agent (git repo)      dashboard :8787            │
        │  adapter.py ← 12 hacks        en/fa RTL · themes · PWA   │
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

---

## 🖥 Dashboard — 23 panels, 60+ API endpoints

A single-file HTML/CSS/JS control plane with glassmorphism, particle canvas, live SSE push, three themes, five accents and **English / فارسی (RTL)** — zero external dependencies, installable as a PWA.

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

### Dashboard features

- **⌘K command palette** — search all 23 panels + actions
- **Themes & languages** — dark / light / auto; indigo / cyan / green / amber / violet; English / فارسی with full RTL (`?lang=fa` or the 🌐 button)
- **SSE live hub** — one `EventSource` on `/api/events` feeds the Console, Logs and status channels with bounded queues + heartbeat
- **PWA offline** — service worker (`dashboard/sw.js`) + webmanifest: the dashboard loads without a network
- **Auth** — per-browser token + optional password gate; secrets never leave the box unmasked
- **Marketplace** — install 17 official Anthropic skills, 14 Superpowers, hermes plugins — into the right store, no arbitrary URLs

---

## 🖥 CLI reference — 27 commands

| Command | Description |
|---|---|
| `atropos setup [--check]` | First-time wizard: detect → check → install → configure |
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

**106 tests, all green** — YAML parser, config roundtrip, settings schema/coercion/migration/secrets, env detection, doctor, 12-hack engine, router + failover logic, extensions, console whitelist (including `rm -rf /` rejection), live HTTP API with auth, SSE. Pure `unittest`, zero dependencies.

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
- **English comments**, فارسی UI strings correct UTF-8 + RTL-aware.
- **Routers = nain · omni · local** — `deepmo` is the model nain serves, never a router.
- **Console /api/run is whitelist-only** — arbitrary shell is forbidden by design.
- **Dry-run only** — Atropos is a control plane, never trades real money.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

*Built to survive redeploys, upstream updates, and its own patches — that's the whole point.*