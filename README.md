# Atropos — the harness that cannot be turned

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6366f1?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6?style=flat-square)](LICENSE)
[![stdlib-only](https://img.shields.io/badge/stdlib-only-22d3ee?style=flat-square)](#rules)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-34d399?style=flat-square)](https://github.com)

> **Atropos** (Ἄτροπος) — the Greek Fate who cuts the thread at the appointed moment: *she who cannot be turned.*

Atropos is a **self-healing control plane** for AI agents running in ephemeral cloud environments. It fuses **Hermes Agent** (orchestration, comms, memory, persistence) with **Claude Code** (coding, debugging, surgery) — one brain, two hands, both kept alive by a single deterministic, stdlib-only harness.

## ✨ Features

- **Self-updating web dashboard** — a single-file HTML/CSS/JS control plane served by a zero-dependency Python HTTP server. No build step, no npm, no CDN. The dashboard polls its own version and offers a one-click reload when an update lands.
- **15-panel control plane** — Overview, Doctor, Patches, Routers, Sessions, Cron, Skills, Plugins, Update, Guest, Logs, Config, Claude, Analytics, History.
- **Declarative patch engine** — 12 YAML-defined hacks, topologically ordered, applied to a git-pristine tree, AST-verified, rollback-safe.
- **Router control (nain / omni / local)** — one command switches the shared model router and propagates it to Hermes `.env` + Claude `settings.json`, with live latency ping.
- **Doctor + auto-fix** — 7+ health checks with one-shot repair.
- **Atomic updater** — fetch upstream → backup → reset → re-apply hacks → doctor verify → rollback on failure. Never auto-restarts; that's always your call.
- **Guest mode + persona editor** — toggle Telegram inline-guest handling and edit ATRA's persona from the web UI.
- **Backup** — timestamped snapshots of config, hacks, and redacted env, keep-last-7.
- **Pure Python stdlib** — runs anywhere Python 3.10+ runs. No dependencies to install, no lockfile to maintain.

## 🚀 Quick start

```bash
git clone https://github.com/arophin/atropos.git
cd atropos

python3 -m py_compile core/*.py atropos   # syntax check
python3 atropos init                      # detect env, write config, apply patches
python3 atropos doctor                    # health checks
python3 atropos route set nain            # pick a router (nain | omni | local)
python3 atropos dashboard                 # start the web dashboard on :8787
```

Open `http://127.0.0.1:8787` and paste the token printed by the dashboard (also at `~/.atropos/auth_token`).

## 🖥 CLI reference

| Command | Description |
|---|---|
| `atropos version` | Show version + runtime |
| `atropos detect` | Detect environment (OS, cloud, hermes, claude) |
| `atropos status` | System status overview + quick doctor |
| `atropos doctor [--fix] [--json]` | Health checks, one-shot auto-fix |
| `atropos route [set <nain\|omni\|local>]` | Show / switch model router |
| `atropos patch [--verify\|--apply]` | Verify or re-apply the 12 hacks |
| `atropos update [--check]` | Check / apply upstream update (rollback-safe) |
| `atropos guest [--status\|--toggle]` | Guest mode status / toggle |
| `atropos config get <key> \| set <key> <value>` | Read / write config |
| `atropos logs [--tail N]` | Tail gateway log |
| `atropos dashboard [--port N]` | Start the web control plane |

## 🏗 Architecture

```
                    ┌───────────────────────────────────────┐
                    │           atropos CLI                 │
                    │  doctor · route · patch · update · …  │
                    └───────┬───────────────┬───────────────┘
                            │               │
              ┌─────────────▼──────┐   ┌────▼────────────────┐
              │  core/  (stdlib)   │   │  dashboard/         │
              │  config  detect    │   │  index.html  (1f)   │
              │  doctor  patches   │   │  served on :8787    │
              │  router  guest     │   └───▲──────────────────┘
              │  update  logs      │       │ /api/* JSON
              └───┬─────────▲──────┘       │
                  │         │              │
        git fetch │         │ reset        │
        (upstream)│         │ + re-apply   │
                  ▼         │              │
   ┌────────────────────────┴───────────┐  │
   │  hermes-agent (git repo)           │◄─┘ reads state.db,
   │  adapter.py ← 12 hacks            │   logs, cron/, skills/
   └────────────────────────────────────┘
```

- **No hardcoded paths** — everything resolves through `core/detect.py` (`HERMES_HOME`, `ATROPOS_HOME`, `$PATH`).
- **Config** lives at `~/.atropos/config.yaml` and also reads live env (`OPENAI_BASE_URL`, `TELEGRAM_LOG_CHANNEL`, …) and the Hermes `config.yaml`.
- **Routers**: `nain` (main — serves the **deepmo** model), `omni` (OpenRouter), `local` (Ollama). `deepmo` is a *model*, not a router.

## ⚡ Patch engine

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

## 🩺 Doctor checks

| # | Check | Auto-fix |
|---|---|---|
| 1 | Python ≥ 3.10 | — |
| 2 | hermes-agent present | clone upstream |
| 3 | PTB ≥ 22.8 | pip reinstall |
| 4 | Claude Code present | — |
| 5 | Patches applied | re-apply |
| 6 | Disk < 85% | — |
| 7 | Timezone Asia/Tehran | — |

## 🧪 Tests

```bash
python3 -m unittest tests/test_core.py -v
```

Covers the YAML subset parser, config roundtrip, env detection, doctor, the 12-hack patch table, router switching (nain/omni/local), and guest mode — pure `unittest`, no pytest.

## 🧩 Templates

`templates/` ships deployment scaffolding: Hermes `.env`, Hermes `config.yaml`, the log-channel hook, the guest persona, and a daily backup cron entry — all with `{{PLACEHOLDER}}` substitution.

## Rules

- **stdlib only** — `unittest`, `http.server`, `sqlite3`, `urllib`, `subprocess`. Nothing else.
- **No hardcoded `/data` or `/opt`** in `core/` — `detect.py` provides every path.
- **English errors**, Persian-friendly UI strings where it counts.
- **Routers = nain · omni · local** — `deepmo` is the model nain serves.

## 📄 License

MIT — see [LICENSE](LICENSE).

---

*Built to survive redeploys, upstream updates, and its own patches — that's the whole point.*
