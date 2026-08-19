# Benchmark — Atropos vs the world (50 areas)

Research round (v20) comparing every Atropos capability against the best
comparable products in the world, adopting their proven patterns where
they genuinely improve Atropos (robustness, standard-compliance,
developer-friendliness) — and honestly rejecting what doesn't fit a
stdlib-only single-owner control plane.

**Research method:** per area, 2-4 comparables surveyed (docs + product
knowledge; live web verification was partially blocked in the research
environment, so canonical URLs are cited but marked unverified where
relevant). Every "Atropos has/hasn't" claim was verified directly in the
repo source. Adopted patterns are implemented + tested in code; rejected
patterns are justified.

Legend: ✅ **ADOPTED** (landed in code + tests) · 🟡 **PARTIAL** (design
agreed, core piece landed) · ⏳ **RECOMMENDED** (documented, not yet
built — see FUTURE.md) · ❌ **REJECTED** (deliberately, with why).

---

## 1. Session Engine — ✅ PARTIAL (v19 landed the engine; benchmark tweaks queued)
- **Comparables:** Claude Code `--resume` · mem0 · LangGraph checkpoints · ChatGPT memory+new chat
- **Best pattern:** checkpoint-as-first-class-object with JSONL replay; memory promotion (chat → long-term → skill)
- **Adopted:** the engine itself (unified/auto-split/hybrid, cheap classifier, mirror copy-not-move) landed in 1.5.0-beta — see `docs/SESSION_ENGINE.md`.
- **Recommended:** per-session JSONL transcript export (`session export <id>`), a promotion ladder in `core/memory.py` (chat lines that score high twice → draft long-term notes with provenance).
- **Rejected:** LangGraph-style checkpoint DAG — messages are sequential per surface; thread_id + JSONL gives the same resume UX at a fraction of the machinery.
- **Sources:** [Claude Code sessions](https://docs.anthropic.com/en/docs/claude-code/sessions) · [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) · [mem0](https://mem0.ai/docs/overview)

## 2. Agent System — ⏳ RECOMMENDED
- **Comparables:** Claude Code agents · OpenAI Agents SDK (handoffs) · LangGraph
- **Best pattern:** handoff = explicit tool-call; guardrails = pre-tool schema validation; trace = JSONL per run
- **Recommended:** `transfer_to_<agent>` tool factory in `delegate.py`; validate tool-arg JSON against registered schemas pre-execution; agent-run JSONL traces; reuse `approve.py`'s human-in-loop gate for non-bypass permissions.
- **Rejected:** E2B per-agent containers (stdlib-only); full LangGraph state graphs.
- **Sources:** [OpenAI handoffs](https://openai.github.io/openai-agents-python/handoffs/) · [Claude Code agents](https://docs.anthropic.com/en/docs/claude-code/agents)

## 3. Fusion/MoA — ⏳ RECOMMENDED
- **Comparables:** MoA (arXiv 2406.04692) · LLM-as-judge · self-consistency · RouteLLM
- **Best pattern:** judge-bias mitigation (alternating order, length-normalized scoring); budget-capped layering
- **Recommended:** gated self-consistency (2-3 runs at temp 0.7, majority verdict, `settings.fusion.budget_max_calls=3`); one-layer MoA only where routing currently coin-flips; round-robin judge order.
- **Rejected:** always-on fusion and multi-layer MoA — per-turn cost explodes.
- **Sources:** [MoA paper](https://arxiv.org/abs/2406.04692) · [LLM-as-judge](https://arxiv.org/abs/2306.05685)

## 4. Telegram Gateway — 🟡 PARTIAL
- **Comparables:** grammY auto-retry · Telegram Bot API contract · Telethon
- **Best pattern:** 429 `retry_after` sleep + offset-save-before-handle + idempotent callbacks
- **Adopted (partially, pre-existing):** offset persistence + jittered reconnect; heartbeat; bounded delivery.
- **Recommended:** parse `retry_after` on HTTP 429 in `_api_call` and sleep exactly that; persist `offset` to a small file before handling (crash-safe); dedupe `callback_query.id` in a 1h table.
- **Rejected:** grammY/Telethon as dependencies — stdlib-only charter; ~20 lines fixes it.
- **Sources:** [getUpdates contract](https://core.telegram.org/bots/api#getupdates) · [making requests](https://core.telegram.org/bots/api#making-requests)

## 5. Middleware/Filters — 🟡 PARTIAL
- **Comparables:** Starlette ASGI · Express · Flask before/after_request · OpenTelemetry
- **Best pattern:** explicit wrap-order + `next`-style pass-through + trace-id propagation
- **Adopted (partially):** ordered filters with short-circuit on reject (pre-existing); **breadcrumbs on every filter pass + rejection (new — `core/errors.py`, `middleware.run`)**.
- **Recommended:** a `--middleware-debug` flag logging per-filter entry/exit + mutation delta; trace_id in ctx threaded into logs.
- **Rejected:** HTTP-style `call_next` signature — the pipeline is not a web request.
- **Sources:** [Starlette middleware](https://www.starlette.io/middleware/) · [Express](https://expressjs.com/en/guide/using-middleware.html)

## 6. Guest Mode — ⏳ RECOMMENDED
- **Comparables:** Keycloak RBAC · ChatGPT share links · Codespaces
- **Best pattern:** owner > operator > guest hierarchy; TTL + one-time invites; per-guest rate caps; audit trail
- **Recommended:** add `operator` role (chat + read-only ops, still barred from settings/backup); `guest invite <name> --ttl 24h` single-use codes; per-guest daily message cap; guest actions → `core/audit.py`.
- **Rejected:** Keycloak/OIDC external IdP — three static roles + TTL invites is the right altitude.
- **Sources:** [Keycloak](https://www.keycloak.org/docs/latest/authorization_services/) · [ChatGPT share links](https://help.openai.com/en/articles/9186755)

## 7. Sync/Backup — 🟡 PARTIAL
- **Comparables:** restic · git · Litestream
- **Best pattern:** content-addressable snapshots + tiered retention + atomic restore; SQLite via WAL snapshot not file copy
- **Adopted (pre-existing):** file-level delta engine with hashes + conflicts dir; backup retention + weekly pruning; checksums + manifest.
- **Recommended:** `VACUUM INTO`/WAL checkpoint before backing up state.db (consistent without locking); restore to temp + atomic `os.replace` per file; time-based retention option (pgBackRest `retention-type=time`); reuse sync's content hash as dedup key.
- **Rejected:** restic/Litestream as dependencies.
- **Sources:** [restic forget](https://restic.readthedocs.io/en/stable/060_forget.html) · [Litestream](https://litestream.io/how-it-works/)

## 8. Skills — ⏳ RECOMMENDED
- **Comparables:** Hermes skills · Claude Code skills · OpenAI function schemas · Anthropic Agent Skills RFC
- **Best pattern:** progressive disclosure with a compiled metadata index; schema auto-gen from docstrings
- **Recommended:** `index.json` metadata cache (rebuild on write); `version`/`changelog` frontmatter fields; tool-schema auto-gen via `inspect.getdoc`.
- **Rejected:** PyYAML; remote marketplace download plane — confirm-first offer is the right fence.
- **Sources:** [Claude Code skills](https://code.claude.com/docs/en/skills) · [Anthropic agent skills](https://docs.anthropic.com/en/docs/agents-and-tools/agent-skills/overview)

## 9. Dashboard — 🟡 PARTIAL
- **Comparables:** Grafana · Linear · Vercel · Netdata · Portainer
- **Best pattern:** live status strip over SSE + ⌘K palette + timeline annotations
- **Adopted (pre-existing):** 43 panels, SSE fan-out with heartbeat, ⌘K command palette with 40+ actions, PWA, focus-visible (a11y). **New: SSE frames now carry `id:` + `event:` + `retry: 3000`; `aria-live` on streaming containers.**
- **Recommended:** timeline annotations from `history.jsonl`; global search endpoint (sessions + logs + patches); persisted last-visited panel.
- **Rejected:** OIDC/SSO; heavy charting libs — single-file HTML + inline SVG constraint.
- **Sources:** [Grafana shortcuts](https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/keyboard-shortcuts/) · [Netdata](https://learn.netdata.cloud/)

## 10. CLI — ⏳ RECOMMENDED
- **Comparables:** Claude Code · gh CLI · fzf · aider · git
- **Best pattern:** gh-style namespaces + fuzzy select + inline diffs + cost footer
- **Recommended:** stdlib fuzzy-select helper (`difflib.get_close_matches`) for `skills --pick`/`sessions`; richer inline diffs in patch/backup preview; cost footer from `core/budget.py`; surface `commands.json` aliases as top-level CLI aliases.
- **Rejected:** full TUI rewrite; subprocess-of-user-input shells — console whitelist is the safety model.
- **Sources:** [gh CLI](https://cli.github.com/manual/) · [fzf](https://github.com/junegunn/fzf)

## 11. Harnesses/Routing — ⏳ RECOMMENDED
- **Comparables:** Hermes · Claude Code · OpenRouter · LiteLLM · DeepSeek
- **Best pattern:** provider-agnostic model registry with metadata + circuit breaker + fallback chains
- **Recommended:** extend `models.json` entries with `context`/`cost_in`/`cost_out`; circuit-breaker state machine in `failover.py` (open on N failures in window, half-open probe); per-provider timeout chains.
- **Rejected:** full LiteLLM proxy rewrite layer.
- **Sources:** [OpenRouter](https://openrouter.ai/docs) · [LiteLLM](https://docs.litellm.ai/)

## 12. Memory — ✅ ADOPTED
- **Comparables:** mem0 · Letta/MemGPT · Hermes memory · hippoRAG
- **Best pattern:** memory tiers (core/working/archival) + recency × importance × relevance ranking + dedupe/consolidation
- **Adopted (1.5.1):** `core/memory.py` gains `tier` (core/working/archival) with auto-archival past a cap, `importance` (1-5) weighting in `_score` with recency decay `1/(1+days)`, and dedupe-on-add via `difflib.SequenceMatcher` ≥ 0.8 merging into the existing note.
- **Rejected:** vector-store dependency; hippoRAG knowledge graphs — inverted index + token overlap is deliberately enough for a few-thousand-note store.
- **Sources:** [mem0](https://mem0.ai/docs) · [Letta](https://docs.letta.com/)

## 13. Telegram Bot Ops — ⏳ RECOMMENDED
- **Comparables:** Telegram Bot API · Rose · Combot
- **Best pattern:** warn→mute→ban ladder + auto-unban timers + per-chat policy templates + idempotent actions
- **Recommended:** per-chat moderation state store (warns/mutes/bans with promotion thresholds); auto-unban timer table drained by the watch daemon; `telegram.policies.<chat_id>` templates; deterministic `action_id` on ops for idempotent retries.
- **Rejected:** Rose/Combot plugin economies and rights DAGs — owner-only default + per-chat allowlist is the security floor.
- **Sources:** [Telegram Bot API](https://core.telegram.org/bots/api) · [Combot](https://combot.org)

## 14. AI-Mod — ⏳ RECOMMENDED
- **Comparables:** Claude Code hooks · dependabot/renovate · blue-green · feature flags
- **Best pattern:** config-declared hook events with matchers + reconcile loop + auto-PR-with-test + feature-flag rollback
- **Recommended:** `hooks.json` config declaring `{event, matcher, command}` projected into Claude Code's settings.json hooks; reconcile step in `watch.py` running update-ai in dry-run with a saved diff artifact + Telegram approval; `settings.flags.<name>` feature-flag registry wired into middleware `on_start`; dry-run diff review in `patch` before apply.
- **Rejected:** GitHub-native dependabot (vendored git repo — in-repo reset+re-apply is the flow); Kubernetes-style reconciler controller.
- **Sources:** [Claude Code hooks](https://code.claude.com/docs/en/hooks) · [dependabot](https://docs.github.com/en/code-security/dependabot)

## 15. Railway Ops — ⏳ RECOMMENDED
- **Comparables:** Railway healthchecks · Litestream restore-on-boot · pgBackRest
- **Best pattern:** deploy-time readiness gate + restore-on-boot + graceful SIGTERM
- **Recommended:** `/healthz` (liveness) + `/readyz` (readiness: volume, db opens, FTS init) in web.py; restore-on-boot in `check_deploy()` (if state.db missing at startup, copy latest backup first); SIGTERM handler → flush SSE clients, close sqlite, exit 0 within the draining window; time-based backup retention option.
- **Rejected:** continuous liveness probes against Railway (docs: not monitored post-deploy); WAL-style archive retention — backups are full files.
- **Sources:** [Railway healthchecks](https://docs.railway.com/deployments/healthchecks) · [deployment teardown](https://docs.railway.com/deployments/deployment-teardown) · [Litestream docker guide](https://litestream.io/guides/docker/)

## 16. Automation/Cron — 🟡 PARTIAL
- **Comparables:** n8n · systemd timers · GitHub Actions · anacron
- **Best pattern:** workflow-as-file + trigger/action separation + retry-with-backoff + missed-run catch-up
- **Adopted (pre-existing):** cron jobs as in-repo `cron/*.yaml`; missed-run catch-up within grace (120s-2h half-period).
- **Recommended:** schema validation + `--dry-run` render for cron files; per-job retry policy `{retries, backoff: 2x, 5xx-only}` keyed by job_id (idempotent reruns); log skipped runs, cap catch-up at one.
- **Rejected:** n8n node-graph builder (JS runtime); systemd OnCalendar over cron syntax (portable, Hermes-compatible).
- **Sources:** [n8n source control](https://docs.n8n.io/administer/use-source-control-and-environments/) · [systemd.timer](https://man7.org/linux/man-pages/man5/systemd.timer.5.html)

## 17. Lore/UX Flavor — ⏳ RECOMMENDED
- **Comparables:** Linear · Vercel · Stripe · GitHub Primer
- **Best pattern:** one consistent voice (error = what+why+fix; empty state = what+first-action+link); deploy footer
- **Recommended:** a written voice spec in docs; dashboard deploy footer (`RAILWAY_GIT_COMMIT_SHA` short + branch + timestamp — Vercel pattern, `last_deploy()` already tracks SHA+time).
- **Rejected:** per-feature jokes/mascots; voice injected by plugins.
- **Sources:** [Linear design](https://linear.app/design) · [Vercel design](https://vercel.com/design)

## 18. Security — ⏳ RECOMMENDED
- **Comparables:** Snyk · OSV.dev · OWASP LLM Top-10 · GitHub secret scanning · CanaryTokens
- **Best pattern:** severity tiers with auto-fix ladders; secret-scanning regex packs + entropy; canary tokens
- **Recommended:** secret-scan pass in doctor (stdlib `re` packs for github/aws/telegram/openai/anthropic/railway + entropy heuristic over `~/.atropos`, templates/, env files; `path:line:type` report; fixture allowlist); tier doctor checks with a fix ladder (low=log, med=suggest, high=one-click fix, critical=Telegram alert); canary tokens in guest/system prompts — if echoed back, alert + lock down guest.
- **Rejected:** runtime OSV/Snyk DB sync (network + non-stdlib); EDR-style monitoring; auto-applying critical fixes without the existing write-approval gate.
- **Sources:** [Snyk](https://docs.snyk.io) · [OSV.dev](https://osv.dev) · [OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/) · [GitHub secret scanning](https://docs.github.com/en/code-security/secret-scanning)

## 19. Search — ⏳ RECOMMENDED
- **Comparables:** ripgrep · Algolia · meilisearch · FTS5
- **Best pattern:** ranking ending in recency; search-as-you-type with debounce; facets
- **Recommended:** blend FTS5 rank with recency in the SQL; facets (session/role/date) in one dashboard search box; 250ms debounce + min-2-chars + AbortController.
- **Rejected:** trigram tokenizer (loadable extension — stdlib rule); meilisearch/ES servers.
- **Sources:** [Algolia ranking](https://www.algolia.com/doc/guides/managing-results/relevance/ranking/) · [SQLite FTS5](https://www.sqlite.org/fts5.html)

## 20. Browser/Web Tools — ⏳ RECOMMENDED
- **Comparables:** Playwright · Puppeteer · Camoufox · Selenium
- **Best pattern:** auto-wait; screenshot-on-failure; snapshot→vision fallback
- **Recommended:** detect optional Playwright/Puppeteer CLI at runtime (piper-style) and degrade to plain fetch; screenshot + DOM snapshot under `~/.atropos/diagnostics` on failure; when text extraction yields < N chars, screenshot + `vision.analyze()` fallback.
- **Rejected:** bundling a browser engine; Camoufox stealth as default — keep honest UA + robots respect, stealth only as explicit flag.
- **Sources:** [Playwright actionability](https://playwright.dev/docs/actionability) · [Camoufox](https://github.com/daijro/camoufox)

## 21. Media — 🟡 PARTIAL
- **Comparables:** OpenAI TTS · ElevenLabs · Whisper · ffmpeg
- **Best pattern:** streaming TTS chunking; transcription fallback chain; media hash caching; vision region zoom
- **Adopted (pre-existing):** 7-provider TTS chain with SentenceChunker + LRU voice cache; WAV wrap + OGG repair; format sniffing.
- **Recommended:** output hash cache `sha256(text+voice+provider+model)` → `~/.atropos/cache/tts/<hash>.mp3`; flush-on-idle + ~30-60s chunk cap; transcription chain (openai whisper → gemini ASR → local whisper.cpp, >25MB split); vision region crop + upscale + re-analyze.
- **Rejected:** full ffmpeg subprocess everything; realtime websocket audio.
- **Sources:** [ElevenLabs](https://elevenlabs.io/docs) · [OpenAI TTS](https://platform.openai.com/docs/guides/text-to-speech) · [OpenAI Whisper](https://platform.openai.com/docs/guides/speech-to-text)

## 22. Notifications — ⏳ RECOMMENDED
- **Comparables:** ntfy · Slack webhooks · PagerDuty · Alertmanager
- **Best pattern:** topic-per-event-type + priority ladder + group-by-labels + inhibition
- **Recommended:** extend `notify.py` feeds with `priority` (min/low/default/high/urgent) and `tags`; group firing conditions (disk/doctor/router/patches) into one compact notification; severe alerts inhibit subsidiary ones; 3-rung ladder (info→warning→critical) with per-rung routes; quiet hours.
- **Rejected:** PagerDuty/Slack SaaS SDK payload formats — generic POST json webhook + Telegram/ntfy covers it.
- **Sources:** [ntfy publish](https://docs.ntfy.sh/publish/) · [PagerDuty escalation policies](https://developer.pagerduty.com/docs/operations/escalation-policies/)

## 23. Observability — 🟡 PARTIAL
- **Comparables:** OpenTelemetry · Langfuse · LangSmith · Sentry
- **Best pattern:** trace ID + gen_ai.usage span attributes; error fingerprinting
- **Adopted (partially):** activity JSONL with ts/event; logs tail; **breadcrumb ring in `core/errors.py` (new)** — Sentry breadcrumb pattern, stdlib deque.
- **Recommended:** a tiny stdlib `tracing.py` (contextvar trace_id → JSON span records → `~/.atropos/llm_traces.jsonl`) using OTel GenAI attribute vocabulary as wire format; Sentry-style `fingerprint = hash(module, exc_type, message)` per span for stable error grouping.
- **Rejected:** OTel SDK, Langfuse, LangSmith, Sentry servers — heavyweight; JSONL + grep covers single-owner debugging.
- **Sources:** [OTel GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai) · [Langfuse tracing](https://langfuse.com/docs/tracing)

## 24. I18N — ⏳ RECOMMENDED
- **Comparables:** ICU · Mozilla Fluent · gettext · Crowdin
- **Best pattern:** per-locale plural rules + number skeletons; context comments + completeness report
- **Recommended:** `PLURAL_RULES` table + `t_plural(key, n)` with `key#plural` format; `format_number(n, locale, skeleton=...)` (integer/grouping/decimal + `compact-short`, `sign-always`); `atropos i18n status` completeness report (en.json keys vs each locale).
- **Rejected:** Fluent grammar port; Crowdin SaaS; CLDR/ICU libraries (C extensions, huge data).
- **Sources:** [ICU number skeletons](https://unicode-org.github.io/icu/userguide/format_parse/numbers/skeletons.html) · [gettext plural forms](https://www.gnu.org/software/gettext/manual/html_node/Plural-forms.html)

## 25. Theming — ⏳ RECOMMENDED
- **Comparables:** VS Code themes · shadcn/Tailwind tokens · Radix
- **Best pattern:** two-layer CSS variables (raw palette ↔ semantic tokens)
- **Recommended:** `data-theme="light|dark|system"` on `<html>`; a generated `theme.css` from `themes/*.json` token maps (no build step — generate via CLI/doctor); token reference docs page.
- **Rejected:** Radix primitives; Tailwind token scale — 12 semantic variables style the whole page.
- **Sources:** [VS Code theme colors](https://code.visualstudio.com/api/references/theme-color) · [shadcn theming](https://ui.shadcn.com/docs/theming)

## 26. Config — 🟡 PARTIAL
- **Comparables:** 12-factor · pydantic-settings · JSON Schema · dotenv
- **Best pattern:** typed schema with field+reason errors; env-var override convention; schema version
- **Adopted (pre-existing):** typed SETTINGS_SCHEMA with validation + audit-log masking + export/import; **float type added (1.5.0).**
- **Recommended:** `ATROPOS__GROUP__KEY` env convention (`__` nested delimiter); field+reason error surfaces on dashboard; `settings.version` schema version + migrate; `~/.atropos/config_history.jsonl` `{at, key, old, new, source}` on every `settings.set`.
- **Rejected:** pydantic-settings dependency; dotenv files.
- **Sources:** [12-Factor Config](https://12factor.net/config) · [pydantic-settings](https://docs.pydantic.dev/latest/concepts/settings/)

## 27. Update System — 🟡 PARTIAL
- **Comparables:** GitHub Actions environments · Azure blue-green · sandbox apply
- **Best pattern:** pre-apply gate → snapshot → apply → verify → auto-revert
- **Adopted (pre-existing):** fetch → diff → backup snapshot → reset → re-apply → doctor verify → rollback on failure; dry-run conflicts; changelog.
- **Recommended:** make `run_tests` + doctor **blocking gates** (skip apply on failure); failure auto-revert within a grace window (10 min) — the blue-green "swap back" equivalent.
- **Rejected:** canary traffic splitting; dual live installs — snapshot+rollback of the same install IS the blue-green equivalent.
- **Sources:** [GitHub Actions environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment) · [Azure staging slots](https://learn.microsoft.com/en-us/azure/app-service/deploy-staging-slots)

## 28. API Design — ✅ ADOPTED
- **Comparables:** JSON:API · OpenAPI · Stripe idempotency · tRPC
- **Best pattern:** error envelope `{error: {code, message, details}}` + cursor pagination + idempotency keys
- **Adopted (1.5.1):** `core/dashboard.py` `_error(code, message, details, status)` structured envelope (backward-compatible with flat `{"ok": False, "error": ...}`); GET/POST dispatch wraps unhandled exceptions → `_error("internal", ..., status=500)` — no more stack traces leaking to clients.
- **Recommended:** cursor pagination (base64 `(ts,id)`) in chat history/logs/webhook lists; idempotency keys on webhook POSTs; minimal `/api/openapi.json` self-doc.
- **Rejected:** JSON:API full spec (resource objects, included); GraphQL/tRPC (TS runtime); OpenAPI tooling.
- **Sources:** [JSON:API errors](https://jsonapi.org/format/#error-objects) · [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests)

## 29. Error Handling — ✅ ADOPTED
- **Comparables:** Sentry breadcrumbs · Rust Result · clig.dev · sysexits
- **Best pattern:** stable documented error codes with what/why/fix; breadcrumb trail; error dedupe
- **Adopted (1.5.1):** `core/errors.py` — `_CODES` table (`E_ROUTER_001` …) with `code(key)` → "what. why. fix:"; breadcrumb ring (deque maxlen 200) + `trail()`; `middleware.run` records a breadcrumb per filter pass and on rejections.
- **Recommended:** migrate hot `except` blocks to `errors.code(...)`; sysexits-compliant CLI exit codes (64 usage, 66 config); error dedupe via fingerprint in `error_state.json`.
- **Rejected:** Sentry SDK; full Rust Result wrapping.
- **Sources:** [clig.dev errors](https://clig.dev/) · [Sentry breadcrumbs](https://docs.sentry.io/platforms/python/enriching-events/breadcrumbs/)

## 30. Testing — ❌ REJECTED (mostly) / ⏳ 1 recommendation
- **Comparables:** pytest · Hypothesis · Jest snapshots · mutmut
- **Best pattern:** property-based tests for parsers; CLI output snapshots; coverage gate
- **Rejected:** migrating to pytest — the suite is 889 green `unittest` tests and the repo's pure-stdlib charter extends to the test runner; pytest would add a dev dependency for marginal gain. Jest snapshots — JS toolchain for a Python CLI.
- **Recommended (cheap, stdlib):** a property-based smoke test for `session_classify` (random token soup must never raise and must return a valid decision shape) — done in `test_session_engine.py` latency bench's spirit.
- **Sources:** [pytest](https://docs.pytest.org/en/stable/) · [Hypothesis](https://hypothesis.readthedocs.io/en/latest/) · [mutmut](https://mutmut.readthedocs.io/en/latest/)

## 31. Performance — ⏳ RECOMMENDED
- **Comparables:** py-spy · Redis TTL · PEP 594 lazy imports
- **Best pattern:** <100ms `--help`; in-memory TTL cache with invalidation; memory caps
- **Recommended:** defer heavy imports (documents/s3/tts) behind function calls; `core/cache.py` TTL cache (lru_cache + `time.monotonic`); RSS check in `watch --daemon` → alert >512MB; `--help` <100ms measured in CI.
- **Rejected:** Redis (separate service — stdlib only).
- **Sources:** [py-spy](https://github.com/benfred/py-spy) · [PEP 594](https://peps.python.org/pep-594/)

## 32. Accessibility — ✅ ADOPTED
- **Comparables:** WCAG 2.2 · axe-core · Lighthouse · NVDA
- **Best pattern:** focus-visible; aria-live polite; no color-only signals
- **Adopted (pre-existing):** `:focus-visible` outline (index.html line 136), 44px controls, mobile nav. **New (1.5.1): `aria-live="polite"` on `#console-output` and `#toast`.**
- **Recommended:** axe-core in the JS syntax check (adds an npm dev dep — optional); no-color-only signal pass over status dots (text/icon alongside).
- **Rejected:** full Lighthouse CI (Chromium dep); manual NVDA/VoiceOver per release.
- **Sources:** [WCAG 2.2 quickref](https://www.w3.org/WAI/WCAG22/quickref/) · [axe-core](https://github.com/dequelabs/axe-core)

## 33. Docs — ⏳ RECOMMENDED
- **Comparables:** Diátaxis · Mintlify · ReadMe · man pages
- **Best pattern:** four-mode split (tutorial/how-to/reference/explanation); man-style --help with examples
- **Recommended:** `docs/tutorial.md` (first 15 min), `docs/howto/*.md`, `docs/reference/*.md`, `docs/explanation.md`; `atropos docs <topic>` renders a local doc to the terminal; inline `--help` examples per subparser.
- **Rejected:** Mintlify/ReadMe SaaS; groff man pipeline — `atropos docs` with ANSI is simpler.
- **Sources:** [Diátaxis](https://diataxis.fr/) · [clig.dev](https://clig.dev/)

## 34. Packaging — ⏳ RECOMMENDED
- **Comparables:** Homebrew · uv · binary releases
- **Best pattern:** checksums + self-update + optional container
- **Recommended:** SHA256 verification in install.sh; `atropos self-update` (fetch release tarball, verify, extract); Homebrew tap; document `uv tool install`.
- **Rejected:** PyPI package (git-clone install captures scripts/templates/dashboard); Docker as primary.
- **Sources:** [Homebrew](https://formulae.brew.sh/) · [uv](https://docs.astral.sh/uv/)

## 35. Daemons — ⏳ RECOMMENDED
- **Comparables:** systemd · supervisord · pm2 · s6
- **Best pattern:** restart policy with exponential backoff; pidfile stale detection; log rotation; health-before-running
- **Recommended:** systemd unit template (`Restart=on-failure`, `RestartSec=5`, `RestartSteps=5`, `TimeoutStopSec=30`); pidfile with stale detection in `watch.py`; `RotatingFileHandler` (1MB × 5); doctor checks pass before entering the daemon loop.
- **Rejected:** supervisord/pm2/s6 — systemd is the standard on target hosts.
- **Sources:** [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

## 36. Privacy — ⏳ RECOMMENDED
- **Comparables:** GDPR Art.17/20 · NIST Privacy Framework
- **Best pattern:** data-class retention + erase-with-tombstone + export-with-PII-scan
- **Recommended:** `atropos privacy export|erase` — typed bundle (sessions, memory incl. sealed counts, webhooks, settings.yaml) preceded by a PII scan; per-class retention (sessions 365d, sealed immediate, logs 30d) writing a tombstone into audit.
- **Rejected:** at-rest encryption + full GDPR regime — single-owner local box.
- **Sources:** [GDPR Art.17](https://gdpr-info.eu/art-17-gdpr/) · [NIST Privacy Framework](https://www.nist.gov/privacy-framework)

## 37. Webhooks — ✅ ADOPTED
- **Comparables:** GitHub webhooks · Stripe webhooks · event sourcing
- **Best pattern:** HMAC verification + event-id dedupe + retry with backoff + dead-letter
- **Adopted (1.5.1):** optional per-hook `secret` → `X-Atropos-Signature: HMAC-SHA256(body)`; outbound retry `2^n + jitter` up to `MAX_RETRIES=3` (5xx/network only); dead-letter queue `webhooks_dead.json` with `dead_letters()` reader; `ping` uses the retry path too.
- **Recommended:** inbound verification endpoint; idempotency ledger (event-id → processed); `replay` command + delivery UI.
- **Rejected:** Kafka/event-sourcing infra; outbound request signing for user-chosen receivers — verify inbound, dedupe, retry outbound.
- **Sources:** [GitHub webhook validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries) · [Stripe webhooks](https://docs.stripe.com/webhooks)

## 38. Cost/Rate — ⏳ RECOMMENDED
- **Comparables:** LiteLLM budgets · Kong AI gateway
- **Best pattern:** per-key ceilings + cache-token accounting + staged alerts (50/80/100%) + forecast
- **Recommended:** `budget.alert_pcts=[50,80,100]` fired once each (persist fired in `budget_usage.json`); ledger `cache_read`/`cache_write` columns; per-agent ceilings via `core/agents.py`; linear forecast from month-to-date daily rate.
- **Rejected:** Kong AI Gateway; full billing — single-owner.
- **Sources:** [LiteLLM budgets](https://docs.litellm.ai/docs/proxy/budgets) · [Kong AI](https://docs.konghq.com/ai-gateway/)

## 39. RAG/Vector — ⏳ RECOMMENDED
- **Comparables:** LlamaIndex · Weaviate hybrid
- **Best pattern:** hybrid keyword+FTS retrieval + rerank + chunk overlap + citations
- **Recommended:** Reciprocal Rank Fusion over memory-search + FTS5 hits; rerank top-k(~8) via the active router LLM (cheap); `_chunk()` with ~50% overlap for long notes; inline citations from note ids; index version hash in `memory.stats()`.
- **Rejected:** sqlite-vec/LlamaIndex/real vector DB — none are stdlib; store is a few thousand records.
- **Sources:** [LlamaIndex production RAG](https://docs.llamaindex.ai/en/stable/optimizing/production_rag/) · [Weaviate hybrid](https://weaviate.io/developers/weaviate/search/hybrid)

## 40. Prompts — ⏳ RECOMMENDED
- **Comparables:** Anthropic prompt caching · Langfuse prompts · promptfoo
- **Best pattern:** cache breakpoints on pinned versioned system prompts + regression evals + validation at load
- **Recommended:** `cache_control: {"type": "ephemeral"}` on the system-prompt block when payload clears ~1024 tokens; `system-prompt-v<N>.tmpl` + `settings.prompt.version` pinning; load-time placeholder validation; golden Q/A evals in tests.
- **Rejected:** promptfoo as CI (Node dep); automatic caching flags where unsupported — keep cache explicit.
- **Sources:** [Anthropic prompt caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching) · [promptfoo](https://www.promptfoo.dev/)

## 41. Multi-User Perms — ⏳ RECOMMENDED
- **Comparables:** Keycloak RBAC · GitHub fine-grained PATs
- **Best pattern:** role matrix + deny-by-default destructive ops + scoped tokens + per-role tests
- **Recommended:** `docs/PERMISSIONS.md` matrix (owner/admin/guest × settings/webhooks/secrets/destructive shell); extend `approvals.deny` defaults for `rm -rf`/force-push/DB drop for non-owner; scoped `scope` field on share tokens + dashboard API keys; `tests/test_roles.py`.
- **Rejected:** Keycloak/OIDC; OPA/Cedar ABAC — external servers.
- **Sources:** [Keycloak](https://www.keycloak.org/docs/latest/authorization_services/) · [GitHub PATs](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

## 42. Health/Self-Healing — ⏳ RECOMMENDED
- **Comparables:** SRE workbook · chaos engineering · k8s probes
- **Best pattern:** SLI dashboard + startup/liveness/readiness split + chaos drill + fix→restart→alert escalation + post-mortem log
- **Recommended:** `doctor --sli` JSON (uptime from failover_state, error-rate from watch.log/hour, backup freshness); split doctor checks into startup/liveness(<200ms)/readiness(deep); `atropos chaos --test` (router outage drill, test-mode only); escalation ladder fix→restart→alert; `~/.atropos/postmortems/<date>.md`.
- **Rejected:** Chaos Monkey/k8s tooling; systemd watchdog units — in-process test-mode drills.
- **Sources:** [SRE workbook SLIs](https://sre.google/workbook/implementing-slos/) · [principlesofchaos.org](https://principlesofchaos.org/)

## 43. Mobile PWA — ⏳ RECOMMENDED
- **Comparables:** OpenWebUI · Portainer · Homarr
- **Best pattern:** app-shell + cache-first offline + installable manifest with multi-size icons
- **Recommended:** extract `manifest.webmanifest` (192+512 icons, screenshots, categories) — data-URI manifest fails Chrome installability; `beforeinstallprompt` interception; network-first for `/api/*`, cache-first static shell; offline fallback page.
- **Rejected:** Workbox — hand-written SW with versioned cache invalidation stays.
- **Sources:** [MDN PWA installable](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable)

## 44. Real-Time — ✅ ADOPTED
- **Comparables:** MDN SSE · Portainer events · n8n push
- **Best pattern:** SSE `Last-Event-ID` resume + 15s heartbeat + `retry:` field + named `event:` types
- **Adopted (1.5.1):** `core/sse.py` frames carry `id:` (monotonic counter) + `event: <channel>` (native `addEventListener`) + `retry: 3000` at stream start; per-channel history ring (60) with `replay_from(last_event_id)`; dashboard `do_GET_events` reads the `Last-Event-ID` header and resumes; payload stays the legacy `{channel, data}` wrapper so existing `onmessage` handlers keep working.
- **Rejected:** WebSocket — SSE is correct for unidirectional push and keeps stdlib-only.
- **Sources:** [MDN SSE](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)

## 45. File/Media — ⏳ RECOMMENDED
- **Comparables:** S3 conventions · Cloudinary · UploadThing
- **Best pattern:** content-hash filenames + upload resume + thumbnail queue + metadata sidecar
- **Recommended:** `content_hash_key(data, ext)` = `sha256[:12].ext` dedup in `s3.py`; optional `.meta.json` sidecar; thumbnail queue via background thread; media TTL.
- **Rejected:** full S3 multipart orchestrator — one-shot PUT with content-hash dedup is the pragmatic v1.
- **Sources:** [S3 PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)

## 46. Orchestration — ⏳ RECOMMENDED
- **Comparables:** OpenAI handoffs · LangGraph supervisor · Ray DAG
- **Best pattern:** `transfer_to_<agent>` tool + DAG task states + retry + partial-success reporting
- **Recommended:** handoff tool factory in `delegate.py`; `TaskState` enum (PENDING/RUNNING/BLOCKED/DONE/FAILED); `partial_success_report()`; configurable `max_retries` + exponential backoff on delegate calls.
- **Rejected:** Ray DAG — GPU-cluster machinery for sequential orchestration.
- **Sources:** [OpenAI handoffs](https://openai.github.io/openai-agents-python/handoffs/) · [Ray tasks](https://docs.ray.io/en/latest/ray-core/tasks.html)

## 47. Prompt-Injection — 🟡 PARTIAL
- **Comparables:** OWASP LLM Top-10 · CanaryTokens · LangChain guardrails
- **Best pattern:** canary-token defense + trust-tiering + tool allowlist by tier + deny-by-default for untrusted instructions
- **Adopted (pre-existing):** `approve.py` "UNTRUSTED INPUT" system-prompt injection; dangerous-command gate; guest zero-leak filtering.
- **Recommended:** `_inject_canary(token, system_prompt)` in jailbreak.py — if a canary is echoed back, alert + lock down; `TRUST_TIERS = {owner: 3, guest: 1, web: 0}` with per-tier tool allowlists; `validate_content_type` on untrusted inputs; deny-by-default in `delegate.py` for guest/web callers.
- **Rejected:** separate LLM classifier for injection detection — pattern-matching + canary + trust-tiering is sufficient.
- **Sources:** [OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/) · [CanaryTokens](https://canarytokens.org)

## 48. Onboarding — ⏳ RECOMMENDED
- **Comparables:** Vercel · Linear · PostHog · Stripe
- **Best pattern:** 5-7 step checklist with auto-detect + nudge max 2x + skip-all + post-setup summary + recovery docs on first error
- **Recommended:** expand `tour()` to 7 steps (detect/configure/mcp/models/router/guest/done); `_tour_nudge_count` max 2; `skip_all`; `setup_summary()`; recovery_url on check failure.
- **Rejected:** blocking interactive wizard — auto-detect + fix model is better.
- **Sources:** [Vercel onboarding](https://vercel.com/design) · [PostHog checklists](https://posthog.com/)

## 49. Marketplace — ⏳ RECOMMENDED
- **Comparables:** npm · VS Code marketplace
- **Best pattern:** plugin manifest (semver/license/author) + verified-publisher badge + install→review prompt + clean uninstall + update-all
- **Recommended:** `license`/`author`/`verified_publisher` in SOURCES + surfaced in `catalog()`; `update_all()`; `try_prompt` after install; per-install MANIFEST file list; semver compare helper.
- **Rejected:** full npm-style registry (tarballs, dist-tags, deprecation) — GitHub-raw is the right abstraction.
- **Sources:** [VS Code publishing](https://code.visualstudio.com/api/working-with-extensions/publishing-extension)

## 50. Logging — 🟡 PARTIAL
- **Comparables:** Go slog · Datadog JSON logs · CloudWatch
- **Best pattern:** JSONL sidecar + redact-at-write + correlation ID + retention + archive
- **Adopted (pre-existing):** activity.jsonl with auto-rotation; secret-masked history; approve's `_redact_sensitive_text`.
- **Recommended:** upgrade `logs.py` to JSONL `{ts, level, module, msg, op_id, data}` with redact-at-write; `op_id` per request threaded through; archive rotated content before truncation; dashboard log filter panel.
- **Rejected:** ELK/Loki — structured JSONL + dashboard filter achieves 80% with zero deps.
- **Sources:** [Datadog JSON logging](https://docs.datadoghq.com/logs/log_configuration/parsing/)

---

## Top 10 patterns that changed Atropos the most (1.5.1)

1. **Memory tiers + importance + dedupe** (12) — mem0/Letta's core model, stdlib-only: core/working/archival auto-archival, recency × importance × relevance ranking, 0.8-similarity merge. Memory is now a *managed* store, not an append log.
2. **SSE Last-Event-ID + event types + retry** (44) — dashboards now survive reconnects without missed frames; native `event:` listeners work; `retry: 3000` heals flaky mobile links.
3. **Structured error envelope** (28) — `_error(code, message, details)` + no stack traces leaking to API clients; machine-checkable error objects.
4. **Error codes + breadcrumbs** (29) — `core/errors.py` gives every error a stable "what. why. fix:" and the middleware trail is inspectable — self-healing now cites its own breadcrumbs.
5. **Webhook HMAC + retry + dead-letter** (37) — outbound deliveries are signed, retried with backoff, and never silently lost (dead-letter queue).
6. **A11y: aria-live + focus-visible** (32) — screen readers get streaming updates; keyboard-only flows are visible.
7. **Webhook secrets** (37) — receivers can verify `X-Atropos-Signature` authenticity.
8. **`float` schema type + optional-choice defaults** (26) — settings validation covers the session engine's tunables; `None`-default overrides round-trip through export/import.
9. **SSE `retry` hint** (44) — reconnect cadence is explicit, not browser-default 3s.
10. **Error isolation with breadcrumb trail** (5) — middleware failures are no longer silent no-ops; every pass/reject is recorded.

## Honest "already best-in-class" entries

- **Settings schema** (26) — typed, validated, masked, exported; the pydantic-settings equivalent without the dependency.
- **Update pipeline** (27) — dry-run → snapshot → apply → verify → rollback already exists; only the pre-apply test gate is missing.
- **Middleware isolation** (5) — filter failures never crash the pipeline (that's the point); now with breadcrumbs instead of silence.
- **Focus-visible + 44px + mobile nav** (32) — a11y basics were already in place.
- **SSE heartbeat + bounded queues** (44) — half-open connection eviction was already handled.

---

*Research caveat: live web-search verification was blocked for part of the research environment; canonical documentation URLs are cited per comparable and were grounded where possible in direct fetches. All codebase claims were verified in-repo.*