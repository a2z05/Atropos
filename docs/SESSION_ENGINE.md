# Session Engine — one session for everything

The Single Session Engine routes every conversation (Telegram, dashboard
chat, CLI REPL, agents) through ONE logical entry, and decides per message
which physical session it belongs to. Two modes plus a hybrid, chosen per
surface, fully configurable through `settings.session_engine.*`.

**The speed guarantee is a HARD requirement:** the reply ALWAYS starts
immediately in the current session — no mode ever waits on an LLM call
before starting the response. Deep classification (when used) is async
and may only *mirror* the exchange afterwards.

## Modes

| Mode | What it does | When to use | Latency |
|---|---|---|---|
| `unified` | One session per surface; topics are thread markers inside it | Everyday chat, anything-to-anything | 0 extra ms |
| `auto-split` | A new session per topic, auto-created/resumed | Lots of distinct projects in one day | 0 ms for ~80% of msgs, ≤3ms for the rest |
| `hybrid` | Unified base; very-confident new topics split into sub-sessions | One main conversation + occasional distinct projects | Same as unified in the common case |

Each mode can be set globally (`session_engine.mode`) or per surface
(`session_engine.surfaces.<telegram|dashboard|cli|agents>`, `off` = engine
bypass for that surface).

## How a message is routed (auto-split)

1. **Cheap classifier** (0-3ms, no LLM, no network): keyword/pattern
   scoring over session titles + topic keywords + a built-in topic
   dictionary (`core/session_classify.py`). Handles 80%+ of messages.
2. **Affinity rule** (0ms): consecutive messages usually continue the
   current session — an unclear topic stays put unless a different
   session is confidently identified.
3. **Confidence gate**: only confident routing (`confidence_threshold`)
   splits; everything else is zero-latency.
4. **Mirror/copy-on-write**: if a later (async deep) check disagrees,
   the exchange is *copied* (never moved) into the target session and
   tagged. Both sessions stay coherent; undo with `/session unmirror`.

In `unified`/`hybrid` the classifier only creates thread markers
(`thread: <topic>`) — topics organize the conversation, not the sessions;
memory stays session-level.

## Tunables (`settings.session_engine.*`)

| Key | Default | Meaning |
|---|---|---|
| `mode` | `unified` | global mode: unified \| auto-split \| hybrid |
| `classifier` | `cheap` | cheap \| deep \| hybrid (deep = LLM per msg, opt-in) |
| `affinity_bias` | `0.8` | how strongly consecutive messages stay in-session |
| `confidence_threshold` | `0.6` | routing gate; above → split, below → stay |
| `mirror_on_deep_switch` | `true` | copy-not-move when a deep switch fires |
| `new_topic_min_messages` | `3` | same-topic msgs before a session is locked |
| `session_titles` | `auto` | auto \| manual \| ask |
| `max_sessions` | `50` | auto-split cap; beyond → reuse oldest non-pinned |
| `hybrid_confidence` | `0.9` | hybrid split gate |
| `hybrid_min_depth` | `25` | hybrid: base session msgs before any split |
| `hybrid_max_split_sessions` | `6` | hybrid: cap on split-out sub-sessions |
| `surfaces.<name>` | unset | per-surface override (or `off`) |

## Where it lives

- `core/session_classify.py` — the cheap classifier (stdlib-only, ~0.1ms
  per message on 10k synthetic messages).
- `core/session_engine.py` — the pipeline (classify → decide → route →
  mirror), thread store, stats, merge/unmirror/pin, graceful degradation
  (falls back to plain single session if chat.db is unavailable).
- Tables in `chat.db`: `threads`, `message_topics`, `mirror_links`,
  `session_meta`.
- Topic dictionary: `~/.atropos/session_topics.yaml` (user-extendable,
  auto-grows from new session keywords).

## Surfaces

- **CLI** — `atropos sessions list|current|threads|route|merge|pin|stats|explain|mode [--surface X]`; REPL `/session`, `/thread <name>`, `/end`; the REPL prompt shows `session-id·thread`.
- **Telegram** — `/session`, `/session explain <msg>`, `/thread <name>`, `/end`; every free-form message routes through the engine before `chat.send`.
- **Dashboard** — Sessions panel gains a Session Engine card (mode select, splits/mirrors/threads stats, per-session threads + mirror badges + route button, explain box). API:
  - `POST /api/session_engine/config` (+ set), `POST /api/session_engine/stats`
  - `POST /api/sessions/route {session_id, surface}`, `POST /api/sessions/merge {a, b}`
  - `POST /api/sessions/detailed`, `POST /api/session_engine/explain {message}`
- **Settings panel** — the `session_engine` group renders automatically
  with mode cards and per-surface overrides.

## Examples

```
$ atropos sessions mode auto-split        # switch the CLI surface
$ atropos sessions list                   # sessions with split/threads
$ atropos sessions explain "deploy the railway app"
[session] message: deploy the railway app
[session] surface: cli  mode: auto-split
[session] cheap classifier: new (deploy, score 0.67, 0.358ms)
[session] affinity bias: 0.8  threshold: 0.6
```

A refresh-rate sanity check ships as a test: the cheap classifier runs
10k synthetic messages and asserts avg < 3ms (measured ~0.1ms).

## Related

- `docs/architecture.md` · `AGENTS.md` (v19 section) · `docs/BENCHMARK.md`
- Sessions themselves: `core/chat.py` (chat.db tables).