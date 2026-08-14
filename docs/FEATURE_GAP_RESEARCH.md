# Atropos Dashboard — Feature Gap Research (2026)

**Scope:** What the best dashboard UIs for server/AI-agent management have, and what
Atropos (single-file HTML+JS+CSS, dark glassmorphism, panels: Overview, Doctor,
Patches, Routers, Sessions, Cron, Skills, Plugins, Update, Guest, Logs, Config,
Claude, Analytics, History) is missing.

**Sources:** Portainer, Heimdall, Homarr, Grafana, OpenWebUI, Langfuse, n8n, Dify,
OpenRouter docs/activity pages, most-commented GitHub issues on those repos (live
fetch), DDG search results. Google blocked JS challenges; GitHub API + raw READMEs
worked.

---

## 1. TOP features we already have (keep)

- **Hash-routed SPA with token auth** (localStorage-only token, hidden by default) — matches the security-conscious pattern of self-hosted dashboards.
- **Overview with live runtime status** (OS, Python, cloud, disk bar, agent state, router grid, "test all routers") — this is the Portainer/Grafana "at-a-glance health" pattern.
- **Doctor panel + one-click fixes** — self-healing visible to the user; very few home dashboards have this, it's a differentiator.
- **Patch management with verify/apply-all** — maps to K8s/CI "check → apply" flows.
- **Router testing with latency results** — a real LLM-specific feature (OpenRouter-style provider status).
- **Cron, Skills, Plugins panels** — the OpenWebUI "manage your agent's organs" pattern.
- **Update + Backup flow** — Homarr/Portainer-style maintenance section.
- **Guest mode + persona editor** — multi-persona control that OpenWebUI only approximates.
- **Logs with filter + tail-size, Config editor (get/set + raw YAML), Claude settings.json editor** — depth admin tools.
- **Sessions + History + Analytics (state.db tables)** — raw data access.

## 2. TOP 12 features we're MISSING (with why)

1. **Live trace / session drill-down viewer (Langfuse's core)** — the #1 thing an AI
   dashboard must have in 2026: click any session/turn and see the full chain
   (prompt → provider → tokens → latency → tool calls → errors), expandable, with
   copy-to-raw. Right now Sessions is a count + list; users cannot answer
   "why did that answer cost $2 / why did it fail".
2. **Cost & token analytics per router/model/session** — OpenRouter's whole dashboard
   is spend graphs. We surface stats but no per-model cost, no trend charts, no
   budget alerts. Without $/model nobody can tune the router.
3. **Provider/router latency & error trends over time (uptime panel)** — Grafana
   pattern. Every router has a live test, but there is no history: which provider
   degrades in the evening, which errors most. Needs persisted samples + small
   client-side charts.
4. **Alerting / notifications** — every dashboard users ask for this (top GitHub
   themes). Doctor-checks-fail, disk > 85%, router down, patch verify fails should
   reach Telegram. Currently updates only surface as a banner.
5. **SSE/live streaming updates** — Portainer/Grafana/n8n all push; we poll. Logs
   panel should tail live, overview should tick without manual refresh.
6. **Keyboard shortcuts & command palette (⌘K)** — OpenWebUI's most-requested
   enhancement (37+ comments). Fast panel switching, run doctor, toggle guest.
7. **One-click backups with retention + restore-from-backup** — we have "Backup now"
   but no list of snapshots, no scheduled backup, no restore button. Heimdall/Homarr
   teach: "make it recoverable, visible, and traversable."
8. **Global search across sessions/logs/cron/skills** — Homarr's top want; we have
   per-panel filters only. One box that finds a log line, a session, a patch.
9. **Multi-user / role-based access (RBAC)** — OAuth/OIDC + RBAC are the top
   feature requests on OpenWebUI, Langfuse (SSO), n8n and Portainer (RBAC). At
   minimum: read-only view key vs admin key; document why full OIDC is out of scope
   for a single-file HTML dashboard.
10. **Mobile/PWA responsiveness** — OpenWebUI sells "responsive + offline PWA";
    single-file glass UI is cheap to make mobile-usable. Top user want.
11. **Rate-limit / quota view** — n8n/OpenWebUI/Langfuse all expose usage limits.
    Atropos gates guest mode; a visible budget ("X msgs / hr used") would close
    the loop between gating and feedback.
12. **Changelog / release notes viewer** — OpenWebUI/Langfuse/most tools ship
    in-app changelogs. The Update panel should show release notes before "Apply".

## 3. UX / design improvements (patterns from the studied tools)

1. **Status color language everywhere** (green/amber/red dots per router, per patch,
   per service) — Grafana/Portainer read instantly. Use consistent accent colors,
   not text states.
2. **Empty states that educate** — Homarr/n8n/Langfuse all show "here's what to do
   first" instead of a bare "No data". A `log-empty` line should include the next
   action + docs link.
3. **Card hierarchy + layout persistence** — let users favorite panels; remember last
   visited panel per browser (we have hash routing; add "resume last panel").
4. **Confirmation & undo for destructive actions** — apply patches, restart, delete.
   The Doctor "Apply Fix" button should show exactly what will change before running.
5. **Toasts with actions** — current toast is passive text; upgrade to actionable
   notifications ("Patches applied → View diff").
6. **Progressive disclosure** — Overview first (health), details on click (drill into
   a router, a session). Too much raw JSON/keys on the Overview hurts scanability.
7. **Density & dark-glass polish** — keep glassmorphism but add: monospace numbers,
   right-aligned numeric columns, subtle trend sparklines, focus rings for
   accessibility. SSO/RBAC usually lands second — prioritize the AI/ops features.

## 4. AI/LLM-specific must-haves (2026)

- **Trace drill-down** (Langfuse): per-turn/agent-step inspection with costs, tokens,
  timing, errors. THE differentiator for an agent dashboard.
- **Token/cost accounting per model & route** (OpenRouter): what the router spends
  per model; drives routing decisions and budget alerts.
- **Prompt/tool/call lifecycle view** — see which skill/plugin/tool each turn used;
  failure attribution becomes possible.
- **Provider uptime & fallback visibility** — when Hermes routes over a down
  provider, the dashboard should show "fell back to X" (routing events log).
- **Agent-run/checklist monitoring** (n8n/OpenWebUI live workflow): for Claude Code
  tasks, show live progress instead of a static "running" state.
- **A "status page" export** — Portainer-style shareable read-only status view for
  guests (guest mode makes this cheap).

---

## Files
- `FEATURE_GAP_RESEARCH.md` (this file) written to `/data/workspace/atropos/docs/FEATURE_GAP_RESEARCH.md`.