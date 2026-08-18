# FUTURE — where Atropos goes next

Working list (not a promise). Each item is stdlib-compatible and follows
the existing rules: no pip, no secrets in code, everything lands as
CLI + dashboard + Telegram.

## Short term (next releases)

- **G section tail**: richer `telegram.*` settings, per-chat `ops_allowed`
  two-step confirm in the bot UI.
- **C: CLI polish** — spinner/status-bar polish pass across all commands,
  palette verification, fate-line on more surfaces.
- **Skills panel** — dashboard panel for the v18 I machinery (lint badges,
  environments, provenance).
- **`test_dashboard_redesign.py` extensions** — Command Center actions
  actually invoke endpoints (not just markup assertions).

## Medium term

- **F/G/I/L completion audit** — auto-improve orchestrate depth, skill
  code-comparison pass, chat rebuild parity with the mobile page.
- **Approval UX** — Telegram notify callback for the gateway approval
  queue (buttons to approve/deny from the phone), per-chat allowlists.
- **Sealed memory surfaces** — owner dashboard panel with counts-only
  view + per-guest retention policy.
- **Migration depth** — per-group kind selection in `apply`, dry-run
  diff rendering in the dashboard, undo of multi-step imports.
- **More Hermes ports** — the remaining tool surface (video/youtube/hue/
  audio currently dry-run) moves to real stdlib implementations where
  feasible without external binaries.

## Longer term

- **Multi-box fleet approval** — route approvals across paired devices.
- **Plugin marketplace** — versioned, signed custom filters distributed
  through the existing `marketplace.py` machinery.
- **i18n completeness** — remaining languages to full key parity; RTL
  verification pass on the dashboard.
- **State export/import** — full `~/.atropos` bundle for zero-config
  migration between machines (superset of `backup` + `migrate`).

## Guardrails (always)

- stdlib only, no pip, no lockfile.
- scissors ✂ ≤ 3 repo-wide (only `core/ascii.py`).
- every feature ships CLI + dashboard + Telegram surface.
- tests stay green; parity tests lock ports to their Hermes sources.
- BETA badge until the next release.
