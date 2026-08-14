# Atropos — the harness that cannot be turned

**Atropos** (Ἄτροπος) is the fusion harness that runs Artan's second self: **Hermes Agent** (orchestration, comms, memory, persistence) + **Claude Code** (coding, debugging, surgery) — one brain, two hands, both self-healing, both auto-updating from source.

The name is not a brand — it's the spec. Atropos is the Greek Fate who cuts the thread at the appointed moment: *she who cannot be turned*. This repo is the source of truth for everything that makes the box survive redeploys, upstream updates, and its own patches.

## What this repo is

- **The control plane** — scripts, runbook, and patch sources that keep the server instance alive and current.
- **The fusion layer** — the exact rules for how Hermes and Claude Code split work and cover each other's weaknesses.
- **The rebuild kit** — after ANY redeploy (which wipes `/opt` and `/data` ephemeral content), this repo is what puts the box back together.

## The division of labor (the fusion)

| Strength | Hermes Agent | Claude Code |
|---|---|---|
| Orchestration, sessions, memory, cron | ✅ owner | — |
| Telegram comms, guest mode, persona | ✅ owner | — |
| Writing real code / multi-file edits | delegates | ✅ owner |
| Debugging root causes | delegates | ✅ owner |
| Self-healing patches (PTB, adapter) | ✅ owner (apply_guest_patch.py) | writes them |
| Source auto-update | ✅ owner (atropos_update.sh) | — |

**Rule:** Hermes never hand-writes complex code; Claude never runs the comms layer. Each covers the other's weakness: Claude's edits don't survive redeploys → Hermes' patch script + this repo rebuild them; Hermes' router latency makes big coding tasks slow → Claude runs them in background.

## Layout

```
atropos/
├── README.md              ← this file
├── AGENTS.md              ← second-self instructions (Hermes + Claude both read)
├── docs/
│   └── architecture.md    ← the fusion design in detail
├── scripts/
│   ├── atropos_update.sh  ← THE core: fetch upstream → dry-run → patch → notify
│   ├── atropos_doctor.sh  ← health checks (PTB, patches, timezone, disk, claude)
│   └── atropos_patch.sh   ← thin wrapper: re-apply all guest patches
└── patches/               ← source-of-truth copies of live patch files
    ├── apply_guest_patch.py
    └── guest_notify.py
```

## The update loop (auto-update from source)

```
upstream (NousResearch/hermes-agent) ──fetch/reset──> /opt/hermes-agent
        ▲                                              │
        │                                              ▼
   atropos_update.sh  ◄── applies ──  apply_guest_patch.py (self-heals 11 patches)
        │                                              │
        └────────── notify Artan ──────◄───────────────┘
```

Every cycle is idempotent: reset to pristine upstream → re-apply patches → verify (AST + tests) → report. If upstream changed and a live restart is needed, it **asks first** — restarts are always Artan's call.
