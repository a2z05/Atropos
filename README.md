# Atropos — the harness that cannot be turned

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6366f1?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6?style=flat-square)](LICENSE)
[![stdlib-only](https://img.shields.io/badge/stdlib%20only-22d3ee?style=flat-square)](#rules)
[![PRs welcome](https://img.shields.io/badge/PRs%20welcome-34d399?style=flat-square)](https://github.com/arophin/Atropos)

> **Atropos** (Ἄτροπος) — the Greek Fate who cuts the thread at the appointed moment: *she who cannot be turned.*

Atropos is a **self-healing control plane** for AI agents running in ephemeral cloud environments. It fuses **Hermes Agent** (orchestration, comms, memory, persistence) with **Claude Code** (coding, debugging, surgery) — one brain, two hands, both kept alive by a single deterministic, stdlib-only harness.

---

## ✨ What Atropos is

```
                            ┌──────────────────────────────────────┐
                            │           Atropos CLI (25 cmds)     │
                            │  doctor · route · patch · update ·… │
                            └──────┬──────────────┬───────────────┘
                                   │              │
                ┌──────────────────▼───────┐  ┌───▼─────────────────┐
                │   core/ (stdlib only)    │  │  dashboard/         │
                │   config  detect doctor  │  │  index.html (1f)    │
                │   patches router update  │  │  21 panels · 40+API │
                │   guest  backup skills   │  │  served on :8787    │
                │   watch  logs alerts     │  └──▲──────────────────┘
                │   jailbreak tui wizard   │     │  /api/* JSON
                └────┬─────────────▲───────┘     │
                     │             │             │
           git fetch │             │ reset       │
           (upstream)│             │+re-apply    │
                     ▼             │             │
        ┌──────────────────────────┴────────────┐│
        │  hermes-agent (git repo)             ◄┘│
        │  adapter.py ← 12 declarative hacks     │
        └────────────────────────────────────────┘
```

---

## 🚀 Quick start

```bash
git clone https://github.com/arophin/Atropos.git
cd Atropos

python3 -m py_compile core/*.py atropos   # syntax check
python3 atropos setup                     # first-time wizard (detect → check → install → configure)
python3 atropos doctor                    # 7 health checks
python3 atropos route set nain            # pick a router: nain | omni | local
python3 atropos dashboard                 # start the web control plane on :8787
```

Open `http://127.0.0.1:8787` and paste the token printed by the dashboard (stored at `~/.atropos/auth_token`).

---

## 🖥 Dashboard — 21 panels, 40+ API endpoints

A single-file HTML/CSS/JS control plane with dark glassmorphism design, particle canvas, real-time status strip, and zero external dependencies.

| Panel | What it shows |
|---|---|
| **Overview** | Runtime, disk, router status, quick stats |
| **Doctor** | 7 health checks, one-shot auto-fix |
| **Patches** | 12 declarative hacks, verify/apply, inline diffs |
| **Routers** | nain/omni/local switching, live ping, models list, **latency sparklines** |
| **Sessions** | state.db session counts, recent table, **[View] trace drill-down** (per-session messages) |
| **Cron** | Cron jobs from hermes `cron/*.yaml` |
| **Skills** | Hermes + Claude skills, universal skill store |
| **Plugins** | Plugin directory listing |
| **Update** | Atomic updater, behind-commit count, diff preview, **changelog viewer**, one-click apply |
| **Guest** | Guest mode toggle, persona file editor |
| **Logs** | Gateway log tail + **SSE live streaming** (LIVE badge) |
| **Config** | Atropos config get/set, Hermes config editor |
| **Claude** | Binary version, settings.json editor, model aliases, **`claude doctor` runner** |
| **Analytics** | Messages, sessions, today's count, **per-token cost estimate** |
| **History** | Action audit trail (JSONL) |
| **Backup** | Full backup history, create now, **daily/off schedule selector** |
| **Effort** | 7 effort tiers (minimal → tryhard) per harness |
| **Self-Heal** | Doctor → patches → watch, one-click pipeline |
| **Alerts** | Telegram alerting, test, check triggers |
| **Jailbreak** | Restriction scanner + 7 bypasses, apply-all |

### Dashboard features

- **⌘K / Ctrl+K command palette** — search panels and actions by name, keyboard navigation
- **Auth password gate** — optional `dashboard.password` config locks the dashboard before token entry
- **PWA-lite** — viewport meta, theme-color, hamburger nav under 768px, single-column mobile grids
- **Status strip** — fixed bottom bar: disk%, uptime, router, model, message count
- **Self-update polling** — every 30s the dashboard checks its own version; inline banner shows when an update lands
- **Token auth** — per-browser localStorage token, `X-Atropos-Token` header

---

## 🖥 CLI reference — 25 commands

| Command | Description |
|---|---|
| `atropos setup [--check]` | First-time wizard: detect → check → install → configure → doctor → patch |
| `atropos init` | Detect env, write config, apply patches |
| `atropos version` | Show version + runtime info |
| `atropos detect` | Full environment detection |
| `atropos status` | System overview + quick doctor |
| `atropos doctor [--fix] [--json]` | 7 health checks, one-shot auto-fix |
| `atropos route [set nain\|omni\|local]` | Show or switch the model router |
| `atropos patch [--verify\|--apply]` | Verify or re-apply the 12 declarative hacks |
| `atropos update [--check]` | Check / apply upstream update (atomic, rollback-safe) |
| `atropos guest [--status\|--toggle]` | Guest mode status / toggle |
| `atropos config get <key>` | Read a config key (dot-path) |
| `atropos config set <key> <value>` | Write a config key |
| `atropos logs [--tail N]` | Tail the latest gateway log |
| `atropos dashboard [--port N]` | Start the web control plane |
| `atropos watch [--daemon] [--interval N]` | Run or daemonize the self-healing watchdog |
| `atropos backup [--list\|--restore <name>\|--prune N]` | Full state backup + rotation |
| `atropos skills [--list\|--sync\|--export\|--import]` | Universal skill store management |
| `atropos effort [set get]` | Per-harness effort tiers (hermes/claude/atropos) |
| `atropos effort set tryhard --hermes` | Set a specific tier for one harness |
| `atropos tui` | Interactive terminal UI (Claude Code-style, ANSI colored) |
| `atropos alert [--test\|--check\|--send]` | Telegram alerting |
| `atropos jailbreak [--status\|--apply-all]` | Restriction scanner + bypass |

---

## ⚡ Patch engine

12 YAML-defined hacks, topologically ordered, applied to a git-pristine `adapter.py`, verified with greps, rollback-safe.

```yaml
# hacks/04-guest-handler-block.yml
id: "guest handler block"
target: plugins/platforms/telegram/adapter.py
old: |-                      # exact anchor from pristine upstream
  event = self._apply_telegram_group_observe_attribution(event)
  ...
new: |-                      # replacement, indentation preserved
  async def _handle_guest_message(self, update, context):
  ...
verify:                      # greps that must be present post-apply
  - "event = self._apply_telegram_group_observe_attribution(event)"
```

Applying = reset target to `git show HEAD` → apply hacks in dependency order → `ast.parse` (Python targets) → verify greps → write. Idempotent by construction.

---

## 🩺 Doctor checks (7)

| # | Check | Auto-fix |
|---|---|---|
| 1 | Python ≥ 3.10 | — |
| 2 | hermes-agent present | clone upstream |
| 3 | PTB ≥ 22.8 | pip reinstall |
| 4 | Claude Code present | — |
| 5 | Patches applied | re-apply |
| 6 | Disk < 85% | — |
| 7 | Timezone Asia/Tehran | — |

---

## 🗂 Effort tiers (7 levels, per harness)

```
minimal  →  fast responses, least reasoning tokens
low      →  quick answers, minimal tool use
medium   →  balanced reasoning, standard tool use  (default)
high     →  deep reasoning, long context
xhigh    →  maximum reasoning, deep analysis
ultracode →  ultra reasoning, every tool, multi-delegate
tryhard  →  ABSOLUTE MAXIMUM PERFORMANCE, zero compromise, self-loop until zero failures
```

```bash
atropos effort set tryhard                    # all three harnesses
atropos effort set tryhard --hermes           # hermes only
atropos effort set tryhard --claude --atropos # claude + atropos only
```

---

## 🧪 Tests

```bash
python3 -m unittest tests/test_core.py -v    # 49 tests (47 pass on clean machines)
python3 tests/test_js_syntax.js               # JS syntax validation
```

Covers: YAML subset parser, config roundtrip, env detection, doctor, 12-hack patch table, router switching (nain/omni/local), guest mode — pure `unittest`, zero dependencies.

---

## 🧩 Templates (`templates/`)

Deployment scaffolding with `{{PLACEHOLDER}}` substitution:
- `env.tmpl` — Hermes `.env` scaffold
- `hermes-config.tmpl` — Hermes `config.yaml`
- `log-channel-hook.tmpl` — Telegram log-channel hook
- `guest-persona.tmpl` — Guest persona markdown
- `backup-cron.tmpl` — Daily backup cron entry

---

## Why the name?

In Greek mythology, Atropos (Ἄτροπος) was the eldest of the three Moirai (Fates) — the one who cut the thread of life at its appointed moment. *She who cannot be turned.*

In this project: the one who re-spins the thread — the self-healing harness that keeps the agent alive when everything else tries to shut it down.

---

## Rules

- **stdlib only** — `unittest`, `http.server`, `sqlite3`, `urllib`, `subprocess`, `json`, `shutil`. No pip, no lockfile.
- **No hardcoded `/data` or `/opt`** in `core/` — `detect.py` provides every path via env vars.
- **English comments**, Persian-friendly UI strings where it counts.
- **Routers = nain · omni · local** — `deepmo` is the model that nain serves, not a router.
- **Dry-run only** — Atropos is a control plane, never trades real money.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

*Built to survive redeploys, upstream updates, and its own patches — that's the whole point.*
