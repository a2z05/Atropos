# This project has been discontinued and no longer works.

~~# Atropos — the harness that cannot be turned~~

~~[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-6366f1?style=flat-square&logo=python&logoColor=white)](https://www.python.org)~~
~~[![License: MIT](https://img.shields.io/badge/License-MIT-8b5cf6?style=flat-square)](LICENSE)~~
~~[![stdlib only](https://img.shields.io/badge/stdlib%20only-22d3ee?style=flat-square)](#-rules)~~
~~[![PRs welcome](https://img.shields.io/badge/PRs%20welcome-34d399?style=flat-square)](https://github.com/a2z05/Atropos)~~

~~> **Atropos** (Ἄτροπος) — the eldest of the three Moirai, the one who cuts the thread at the appointed moment: *she who cannot be turned.*~~

~~Atropos is a **self-healing control plane** for AI agents running in ephemeral cloud environments. It fuses **Hermes Agent** with **Claude Code** into one deterministic, pure-stdlib harness.~~

~~---~~

~~## ✨ Highlights~~

~~| | |~~
~~|---|---|~~
~~| **🧠 Single Session Engine** | Unified routing across Telegram, dashboard chat, and CLI. |~~
~~| **🛡 Dangerous-command gate** | Hermes parity approval middleware. |~~
~~| **🖥 43-panel dashboard** | SSE, PWA, themes, RTL, command palette. |~~
~~| **⛓ Three-mode resources** | shared / per-harness / atropos-only. |~~
~~| **🧪 Hermes parity** | Search, vision, TTS, cron, kanban, safety, docs. |~~
~~| **🔁 Self-healing** | Watchdog, patch engine, rollback, backups. |~~
~~| **🔒 Sealed guest memory** | Guest content is isolated by design. |~~
~~| **📦 One-liner install** | `curl ... | sh` |~~

~~---~~

~~## 🚀 Quick start~~

~~```bash~~
~~git clone https://github.com/a2z05/Atropos.git~~
~~cd Atropos~~
~~python3 atropos setup~~
~~python3 atropos doctor~~
~~python3 atropos dashboard~~
~~```~~

~~Open `http://127.0.0.1:8787` and create a dashboard password.~~

~~---~~

~~## 🗺 Architecture~~

~~```~~
~~CLI~~
~~ ↓~~
~~core/~~
~~ ↓~~
~~Dashboard + Hermes~~
~~```~~

~~---~~

~~## 🧵 Single Session Engine~~

~~Three modes: unified, auto-split, and hybrid, configurable through `settings.session_engine.*`.~~

~~---~~

~~## 🛡 Trust & Safety~~

~~- Approval gate is fail-closed~~
~~- Atomic updates with rollback~~
~~- Manifest-based backups~~
~~- Stable `/api/*` contract~~

~~---~~

~~## 📖 Documentation~~

~~- architecture.md~~
~~- SESSION_ENGINE.md~~
~~- BENCHMARK.md~~
~~- CHANGELOG.md~~
~~- AGENTS.md~~
~~- FUTURE.md~~

~~---~~

~~## 🧪 Tests~~

~~909 unittest-based tests, zero external dependencies.~~

~~---~~

~~## ⚙ Rules~~

~~- stdlib only~~
~~- No hardcoded paths~~
~~- Routers: nain / omni / local~~
~~- Whitelist-only console~~
~~- Dry-run only~~

~~---~~

~~## 🤝 Contributing~~

~~PRs welcome.~~

~~---~~

~~## 📄 License~~

~~MIT~~

~~---~~

~~*Built to survive redeploys, upstream updates, and its own patches.*~~
