# Changelog

All notable changes to Atropos are tracked here.

## [1.2.0] — 2026-08-15 (current)

### Added
- **Settings hub (`core/settings.py`)** — single source of truth for every config key. Typed schema with defaults, groups, choices, ranges and secret flags; `settings get/set` with strict coercion (rejects `dashboard.port abc`); one-time migration of legacy nested `alerts.thresholds`; secret masking in every read path (export, API, history log); YAML export/import. New **Settings panel** (22nd) with grouped table, inline editors, theme/lang/accent pickers, export/import buttons; new `atropos settings` CLI (table + get/set/export/import). All modules now read through `settings.py` (`watch`, `alerts`, `backup`, `dashboard`, `effort`…).
- **Universal extension layer (`core/extensions.py`)** — one abstraction over Hermes plugins (`plugin.yaml`), Hermes skills and Claude skills: unified list/enable/disable/install/remove with identifier validation (no path traversal), reversible disable (rename), remove-to-trash. CLI gains `atropos skills --enable/--disable/--remove` and `atropos plugin`; backup retention governed by `backup.retention`.
- **Marketplace tab (`core/marketplace.py`)** — hardcoded allowlist of trusted registries: Anthropic Skills (17), Superpowers by obra (14), hermes-agent plugins (5 with manifests); per-item install state, concurrent bounded description fetching, SSRF-proof hosts, 5MB per-file cap; **Market panel** with one-click Install/Remove; install fetches into the correct local store.
- **Console tab (`core/console.py`)** — safe REPL: whitelist-only dispatcher (`doctor`, `self-heal`, `backup create/update`, `update check/apply`, `route`, `skills`, `plugin`, `settings`, `effort`), identifier-validated args, serialized side-effecting runs, history to `~/.atropos/console_history.jsonl`; `POST /api/run`; `rm -rf /` and any shell is rejected by construction; output broadcasts over SSE to the Console panel.
- **Router auto-failover (`core/failover.py`)** — nain → omni → local on consecutive failures (`failover.retries`), persisted state, Telegram alert on switch, terminal `all_down`, manual-choice grace (`failover.hold_minutes`), wired into the watch daemon, `router.set_active` records manual holds.
- **SSE live hub (`core/sse.py`)** — `/api/events` feeds all panels (status/logs/console/notify channels), bounded per-client queues, heartbeat keepalive, client cap; console output streams live.
- **Session search + export** — `GET /api/sessions/search?q=` (LIKE, bounded 50) and `GET /api/sessions/export` (JSON, bounded 200).
- **PWA true offline** — `dashboard/sw.js` service worker (cache shell, serve offline, never cache `/api/*`), inline webmanifest, apple-mobile meta.
- **TUI 2.0** — arrow-key navigation, `~/.atropos/tui_history`, quick-open digits, new Effort + Extensions panels.
- **Changelog auto-bump** — `update.bump_changelog()` prepends an entry after apply (gated by `update.changelog_bump`).
- **Watch daemon** — threshold/interval/state-db/log caps and auto-backup come from settings; failover check inside `run_watch`.
- **Tests** — `tests/test_settings.py` (21), `tests/test_failover.py` (10), `tests/test_api.py` (26, live HTTP with auth + console whitelist + SSE) — **106 total, all green**; `run_tests.py` discovers all `test_*.py`.
- **Router/doctor hardening** — doctor disk/timezone checks survive missing home dirs and Windows; `api_config_set` validated through the schema with secret masking.

### Changed
- Dashboard now has **23 panels** (Market, Console, Settings added) with **themes** (dark/light/auto), **accents** (indigo/cyan/green/amber/violet) and **language** (English / فارسی RTL, `?lang=fa`, 🌐 toggle) — translation dictionary covers nav, titles, doctor checks, effort tiers and jailbreak names.
- `core/config.py` DEFAULTS stays as the legacy compat layer; `core/settings.py` is layered on top (back-compat preserved).

## [1.1.0] — 2026-08-15

### Added
- **Dashboard update lifecycle (native, no cron)** — `Check now` button shows behind-count, truncated git diff preview, and a banner with one-click **Apply** after an update check reports commits behind. Last-check timestamp persisted in `~/.atropos/update_state.json`.
- **Changelog viewer** — new `docs/CHANGELOG.md` + `GET /api/changelog`; the Update panel shows the changelog inline before applying.
- **Session trace drill-down** — `GET /api/session/{id}` returns the last 20 messages (500-char truncation each) from state.db; Sessions panel rows get a **[View]** button opening a slide-over message timeline (role, timestamp, length, expandable content).
- **Cost/token analytics** — `GET /api/analytics/cost` estimates spend from state.db token columns (graceful `available: false` when absent); Analytics panel gains a per-router `spend` card.
- **Router latency history** — live router tests append samples to `~/.atropos/router_history.json` (capped 200); `GET /api/router/history` feeds inline-SVG sparklines on each router card.
- **Backup schedules (dashboard-native, no cron)** — `backup.period: daily|off` config; `watch.py` now auto-creates a backup when period=daily and the newest backup is older than 24h; Backup panel gets a period selector + count/newest/age.
- **Command palette (⌘K / Ctrl+K)** — searchable overlay for all 21 panels + actions, pure JS, keyboard navigation (Arrows/Enter/Esc).
- **PWA-lite mobile pass** — viewport + theme-color meta, hamburger sidebar under 768px, single-column grids on mobile, CSS-only.
- **Auth hardening** — optional `dashboard.password` config adds a server-side password gate before the token field (checked via `POST /api/auth`); token stays per-browser in localStorage.
- **Claude doctor runner** — Claude panel gains "Run claude doctor" → `POST /api/claude/doctor` → output tail inline.
- README fully rewritten for a GitHub showcase (badges, ASCII architecture diagram, 21-panel table, 25-command cheatsheet, FAST).

## [1.0.0] — 2026-08-14 (initial)

### Added
- **Core modules (`core/`, 16 files)** — detection, stdlib YAML subset parser, doctor (7 checks), router abstraction (nain/omni/local), declarative hack engine (12 hacks), atomic updater with rollback, stdlib HTTP dashboard, guest mode + persona, log tailing, self-healing watchdog, full backup with rotation, universal skill store, terminal UI, setup wizard, Telegram alerting, restriction scanner + 7 jailbreak bypasses.
- **CLI (`atropos`, 25 commands)** — doctor, status, version, route, patch, update, guest, logs, config, watch, backup, skills, effort (per-harness tiers), tui, setup, alert, jailbreak, detect, init, dashboard.
- **Dashboard (`dashboard/index.html`, 21 panels)** — overview, doctor, patches, routers, sessions, cron, skills, plugins, update, guest, logs, config, claude, analytics, history, backup, effort, self-heal, alerts, jailbreak. Dark slate/indigo glassmorphism, particle canvas, hash routing, token auth, toasts.
- **Tests** — 49 tests (47 pass on clean machines, 2 env-dependent).
- **Templates** — env, hermes-config, log-channel-hook, guest-persona, backup-cron.
- **Docs** — README, AGENTS.md, docs/architecture.md, docs/FEATURE_GAP_RESEARCH.md.