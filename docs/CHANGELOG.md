# Changelog

All notable changes to Atropos are tracked here.

## [1.1.0] — 2026-08-15 (current)

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