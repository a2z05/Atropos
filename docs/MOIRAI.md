# The Moirai — why Atropos is named Atropos

## The three sisters

In Greek mythology the **Moirai** (Μοῖραι, "the Apportioners") are the three
Fates who weave, measure, and cut the thread of every mortal life:

| Sister | Role | In Atropos |
|---|---|---|
| **Clotho** (Κλωθώ, "the Spinner") | Weaves the thread of life | **Hermes Agent** — orchestration, sessions, memory, comms |
| **Lachesis** (Λάχεσις, "the Apportioner") | Measures the thread, allocates the share | **Claude Code** — writes and debugs the code, apportions the work |
| **Atropos** (Ἄτροπος, "the Unturnable") | Cuts the thread at the appointed moment | **Atropos itself** — self-healing, patches, updates, the harness that cannot be turned |

## Why the name

Most stories about the Fates end with the cut: Atropos, the eldest, decides
*when*. But Atropos the harness inverts the story. She does not cut to end —
she cuts to **re-weave**. When a thread frays (a broken adapter, a failed
patch, an update gone sideways), she snips the broken strand and spins a new
one. The *unturnable* part is the promise: whatever the world throws at the
thread — redeploys, upstream rewrites, its own patches — the harness cannot
be turned away from its work.

> *Clotho spins. Lachesis measures. Atropos decides.*

The three names are **user-facing branding only**. Technical identifiers —
router names (`nain`/`omni`/`local`), endpoints, config keys, file names —
never change. The Moirai are the story we tell about the machinery, not the
machinery itself.

## The loom

```
   Clotho (Hermes)        Lachesis (Claude)
        │                       │
        └──────────┬────────────┘
                   ▼
              Atropos
   detection · doctor · patches · updates · watch
```

Every thread woven by the agents passes through Atropos: the harness checks
it, patches it, backs it up, and if it breaks — cuts and re-weaves.

## What the Fates are not

- Not deities — they are three processes with different jobs.
- Not a hierarchy — Clotho does not outrank Lachesis; the routing hub
  (`core/routing.py`) decides *which sister* handles *which task*.
- Not magic — everything is deterministic, stdlib-only, and testable.
  The thread is cut only when the work is done.