# Atropos — the harness that cannot be turned

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6366f1?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6?style=flat-square)](LICENSE)
[![stdlib only](https://img.shields.io/badge/stdlib%20only-22d3ee?style=flat-square)](#-rules)
[![909 tests](https://img.shields.io/badge/tests-909%20green-34d399?style=flat-square)](#-tests)
[![PRs welcome](https://img.shields.io/badge/PRs%20welcome-34d399?style=flat-square)](https://github.com/a2z05/Atropos)

> **Atropos** (Ἄτροπος) — the eldest of the three Moirai, the one who cuts the thread at the appointed moment: *she who cannot be turned.*

Atropos is a **self-healing control plane** for AI agents running in ephemeral cloud environments. It fuses **Hermes Agent** (orchestration, comms, memory, persistence) with **Claude Code** (coding, debugging, surgery) — one brain, two hands — kept alive by a single deterministic, **pure-stdlib** harness.

No pip. No lockfile. No node_modules. Just `unittest`, `http.server`, `sqlite3`, `urllib`, and `shutil`.

---

## ✨ Highlights

| | |
|---|---|
| **🧠 Single Session Engine** | One logical entry for every conversation — unified/auto-split/hybrid routing across Telegram, dashboard chat, and the CLI, with zero added latency in the common path. |
| **🛡 Dangerous-command gate** | 77 dangerous + 12 hardline patterns byte-identical to Hermes' `approval.py`, enforced as a middleware filter that never blocks hardline actions — not even in `off` mode. |
| **🖥 43-panel dashboard** | Token-gated web control plane with SSE live push, ⌘K command palette, 9 themes, 11 languages (RTL), PWA. |
| **⛓ Three-mode universal resources** | Every resource (MCP servers, models, skills, webhooks…) has one canonical copy and three deployment modes: `shared`, `per-harness`, `atropos-only`. |
| **🧪 Hermes parity ports** | search, web, X, Home Assistant, TTS (7-provider chain), vision, imagine, cron, kanban, approve, safety, documents, railway ops, live sync — each with hermetic parity tests. |
| **🔁 Self-healing** | Watch daemon, patch engine (12 hacks), atomic update with rollback, router failover, backup + manifest + retention, doctor checks. |
| **🔒 Sealed guest memory** | Guests record notes; the owner sees counts only — content never leaks by construction. |
| **📦 One-liner install** | `curl -fsSL https://raw.githubusercontent.com/a2z05/Atropos/main/install.sh \| sh` |

---

## 🚀 Quick start

```bash
git clone https://github.com/a2z05/Atropos.git
cd Atropos

python3 -m py_compile core/*.py atropos   # syntax check
python3 atropos setup                     # first-time wizard
python3 atropos doctor                    # 7 health checks
python3 atropos route set nain            # pick a router: nain | omni | local
python3 atropos dashboard                 # web control plane on :8787
```

Open `http://127.0.0.1:8787` — the first visit asks you to create a dashboard password (stored salted + hashed at `~/.atropos/dashboard_auth.json`; never in plaintext). Log in with it from any browser, including on Railway deployments.

**Requirements:** Python 3.10+ — nothing else. Zero pip dependencies.

### One-liner install

```bash
curl -fsSL https://raw.githubusercontent.com/a2z05/Atropos/main/install.sh | sh
```

Idempotent — re-running pulls the latest `main`. Needs Python 3.10+ and git.

### Install from anywhere (PATH)

```bash
python3 atropos install              # symlinks/copies `atropos` into ~/.local/bin
atropos setup --check                # verify the environment
```

---

## 🗺 What it looks like

```
                            ┌──────────────────────────────────────────┐
                            │           Atropos CLI (70 cmds)          │
                            │  doctor · route · patch · update · …     │
                            │  settings · skills · sessions · approve  │
                            └──────┬───────────────────┬───────────────┘
                                   │                   │
                ┌──────────────────▼────────────────────▼──────────┐
                │                core/ (stdlib only)               │
                │  settings  config  detect  doctor  extensions    │
                │  session_engine  session_classify  approve       │
                │  patches  router  update  guest  backup  skills  │
                │  watch  logs  alerts  sse  tui  wizard  errors   │
                └───────┬───────────────────────────────▲──────────┘
                        │                               │
                 git fetch │            ┌───────────────┘
                 (upstream)│            │ dashboard/ (index.html + sw.js)
                        ▼               │  43 panels · 90+ APIs · SSE
        ┌───────────────────────────────┴──────────────────────────┐
        │  hermes-agent (git repo)      dashboard :8787            │
        │  adapter.py ← 12 hacks        11 langs · 9 themes · PWA  │
        └──────────────────────────────────────────────────────────┘
```

---

## 🧵 The Single Session Engine

Every conversation (Telegram, dashboard chat, CLI REPL, agents) flows through **one logical entry** that decides per message which session it belongs to — and the reply always starts before any deep classification finishes.

- **unified** — one session per surface; topics are thread markers inside it. **0 extra ms.**
- **auto-split** — a new session per topic, routed by a stdlib keyword classifier (~0.1 ms/message). **0 ms for ~80% of messages.**
- **hybrid** — unified by default; very-confident new topics split into sub-sessions.

All tunables live in `settings.session_engine.*` with per-surface overrides; the Settings panel explains each mode with live examples. Surfaced as `atropos sessions`, Telegram `/session`, and the dashboard Sessions panel.

→ **Read more:** [`docs/SESSION_ENGINE.md`](docs/SESSION_ENGINE.md)

---

## 🛡 A few things that make it trustworthy

- **The approval gate is a floor, not a filter.** Hermes' 77 dangerous + 12 hardline patterns are enforced byte-identical, `approvals.mode: off` never bypasses hardline actions, and headless middleware rejects **fail-closed** — a machine with no human to ask says no.
- **Updates are atomic.** fetch → backup snapshot → reset → re-apply hacks → doctor verify → rollback on any failure. Dry-run conflicts come before apply, the changelog is shown, and a failed doctor auto-restores the previous state.
- **Backups carry manifests.** Full state tar with checksummed `MANIFEST.json`, token files masked, restore-to-temp-then-atomic-replace.
- **The dashboard API is the contract.** `/api/*` endpoints are the surface every panel consumes; the redesign rules say never break them.

---

## 📖 Documentation

| Doc | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | system layout, decisions, counts |
| [`docs/SESSION_ENGINE.md`](docs/SESSION_ENGINE.md) | the Single Session Engine — modes, tunables, examples |
| [`docs/BENCHMARK.md`](docs/BENCHMARK.md) | 50-area research audit vs the world: what was adopted, what was rejected, why |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | release history |
| [`AGENTS.md`](AGENTS.md) | agent identity + the fusion rule |
| [`docs/FUTURE.md`](docs/FUTURE.md) | the roadmap |

---

## 🧪 Tests

```bash
python3 run_tests.py              # or: python3 -m unittest discover tests
node tests/test_js_syntax.js      # dashboard JS syntax + panel consistency
```

**909 tests, all green** — pure `unittest`, zero dependencies: YAML parser, config roundtrip, settings schema/coercion/migration/secrets, env detection, doctor, the 12-hack patch engine, router + failover, extensions, console whitelist (including `rm -rf /` rejection), live HTTP API with auth, SSE (with Last-Event-ID resume), the Session Engine (classifier latency bench: 10k messages ≈ 0.1 ms each), middleware filters incl. the Hermes approval gate, agents, Telegram gateway, Hermes parity matrix (78 tests incl. 77 dangerous + 12 hardline patterns verbatim), guest isolation, sealed memory, migration import, benchmark adoptions (memory tiers, error codes, webhook HMAC), dashboard controls + mobile markup.

---

## ⚙ Rules

- **stdlib only** — `unittest`, `http.server`, `sqlite3`, `urllib`, `subprocess`, `json`, `shutil`. No pip, no lockfile, not even in CI.
- **No hardcoded paths in `core/`** — `detect.py` owns every path via env vars.
- **Routers = nain · omni · local** — `deepmo` is the model nain serves, never a router.
- **Console `/api/run` is whitelist-only** — arbitrary shell is forbidden by design.
- **Dry-run only** — Atropos is a control plane, never trades real money.
- **BETA badge** — version-flagged builds show it; `settings.beta_badge` off hides it.

---

## 🤝 Contributing

PRs welcome. Everything is pure stdlib Python 3.10+, tests live in `tests/`, and every new CLI command must register in three places (subparsers, handlers dict, `tests/test_parity.py` CLAIMED) — that test is the contract.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

*Built to survive redeploys, upstream updates, and its own patches — that's the whole point.*