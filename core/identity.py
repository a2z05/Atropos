#!/usr/bin/env python3
"""Atropos universal identity — the canonical copy of every identity artifact.

THE CORE LAW: one canonical copy of each identity artifact lives in
``~/.atropos/identity/``: SOUL.md, AGENTS.md, SYSTEM.md, GUEST.md,
CODE_STYLE.md, plus the ``prompts/`` subdir (welcome.md, hook_template.md,
log_channel.md, guest_greeting.md, telegram_auto_reply.md). Harness copies
are *projections* of the canonical store — never the other way round.

Three deployment modes, per artifact:

  * ``shared``       — the Atropos copy is canonical; projections are
                       written to the mapped harness locations ONLY on
                       explicit save() / sync() calls.
  * ``separate``     — harnesses keep their own copies; Atropos only
                       monitors them (list / diff / restore still work).
  * ``atropos-only`` — the artifact lives only in Atropos; it is NEVER
                       projected and NEVER overwritten.

The deployment map is stored in settings ``identity.map`` as
``{file: {targets: {harness: path-or-{path, key}}, mode}}``. When
``identity.map`` is empty the built-in DEFAULT_MAP applies (see
default_map()). The map is persisted through settings only when the user
actually changes it (mode(), import_file()).

CONFLICT POLICY (the universal rule): a projection never silently
overwrites a live file that differs from the last content Atropos wrote.
When a target has drifted — or exists with unknown origin and differs
from the canonical content — save()/sync() return a conflict dict and
leave the target untouched. The caller resolves it via
resolve_conflict(name, target, action) with action ``overwrite`` (Atropos
wins), ``keep`` (the harness state becomes the acknowledged baseline) or
``diff`` (compare without writing).

Every save() snapshots the previous canonical content into
``identity/.history/<name>-<ts>`` (last 8 kept per artifact); restore()
brings any snapshot back over the canonical copy. detect_new() proposes
imports of harness identity files that are not registered yet.
"""
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, settings

MODES = ("shared", "separate", "atropos-only")
IDENTITY_FILES = ("SOUL.md", "AGENTS.md", "SYSTEM.md", "GUEST.md", "CODE_STYLE.md")
PROMPT_FILES = (
    "welcome.md", "hook_template.md", "log_channel.md",
    "guest_greeting.md", "telegram_auto_reply.md",
)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HISTORY_KEEP = 8
WRITTEN_FILE = ".written.json"


# ── paths ─────────────────────────────────────────────────────────────────
def identity_dir() -> Path:
    """Canonical store root (~/.atropos/identity)."""
    return detect.atropos_home() / "identity"


def prompts_dir() -> Path:
    """Prompt-templates subdir of the canonical store."""
    return identity_dir() / "prompts"


def history_dir() -> Path:
    """Snapshot history subdir of the canonical store."""
    return identity_dir() / ".history"


def canonical_path(name: str) -> Path:
    """Canonical location of one artifact (prompts live in prompts/)."""
    if name in PROMPT_FILES:
        return prompts_dir() / name
    return identity_dir() / name


def valid_name(name: str) -> bool:
    """True when ``name`` is a safe artifact identifier (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name))


def _claude_home() -> Path:
    """Claude Code home (~/.claude)."""
    return detect._home() / ".claude"


def _guest_persona_target() -> Path:
    """Resolved Hermes guest-persona path (settings override or assets default)."""
    raw = settings.get("guest.persona_path", "") or ""
    if raw:
        p = Path(raw)
        if str(p) != ".":
            return p
    return detect.hermes_home() / "assets" / "guest_persona.md"


def _guest_persona_configured() -> bool:
    """True when a Hermes guest persona is plausibly in use."""
    raw = settings.get("guest.persona_path", "") or ""
    if raw and str(Path(raw)) != ".":
        return True
    return (detect.hermes_home() / "assets").exists()


# ── the deployment map ────────────────────────────────────────────────────
def default_map() -> dict:
    """Built-in deployment map: artifact → {targets, mode}.

    Targets resolve against the live environment at call time (Hermes
    home, Claude home, repo root), so env overrides are honoured.
    SYSTEM.md projects into the Hermes config.yaml under the
    ``system_prompt`` key (keyed target); GUEST.md projects to the
    configured guest persona path when one exists.
    """
    repo = Path(__file__).resolve().parent.parent
    return {
        "SOUL.md": {
            "targets": {
                "hermes": str(detect.hermes_home() / "SOUL.md"),
                "claude": str(_claude_home() / "CLAUDE.md"),
            },
            "mode": "shared",
        },
        "AGENTS.md": {
            "targets": {"repo": str(repo / "AGENTS.md")},
            "mode": "shared",
        },
        "SYSTEM.md": {
            "targets": {
                "hermes": {
                    "path": str(detect.hermes_home() / "config.yaml"),
                    "key": "system_prompt",
                },
            },
            "mode": "shared",
        },
        "GUEST.md": {
            "targets": ({"hermes": str(_guest_persona_target())}
                        if _guest_persona_configured() else {}),
            "mode": "shared",
        },
        "CODE_STYLE.md": {
            "targets": {
                "hermes": str(detect.hermes_home() / "CODE_STYLE.md"),
                "claude": str(_claude_home() / "CODE_STYLE.md"),
            },
            "mode": "shared",
        },
    }


def _effective_map() -> dict:
    """DEFAULT_MAP merged with the user's ``identity.map`` overrides."""
    m = default_map()
    user = settings.get("identity.map", {})
    if isinstance(user, dict):
        for k, v in user.items():
            if isinstance(v, dict):
                m[k] = v
    return m


def _persist_map(m: dict):
    """Persist the merged deployment map into settings (user-changed)."""
    settings.set("identity.map", m)


def _map_entry(name: str):
    """Effective deployment entry for an artifact (override or default)."""
    return _effective_map().get(name)


def _default_targets(name: str) -> dict:
    """Built-in targets for an artifact (empty for prompts)."""
    entry = default_map().get(name)
    return dict(entry["targets"]) if entry else {}


def _mode_for(name: str) -> str:
    """Effective deployment mode of an artifact."""
    entry = _map_entry(name)
    if entry and "mode" in entry:
        return entry["mode"]
    if name in PROMPT_FILES:
        return "atropos-only"
    if name in IDENTITY_FILES:
        return "shared"
    return "separate"


def _targets_for(name: str) -> dict:
    """Effective mapped targets of an artifact."""
    entry = _map_entry(name)
    if not entry:
        return {}
    return entry.get("targets") or {}


# ── target helpers ────────────────────────────────────────────────────────
def _target_path(spec) -> Path:
    """File path of a target spec (plain path string or {path, key} dict)."""
    if isinstance(spec, dict):
        return Path(spec["path"])
    return Path(spec)


def _target_keyed(spec) -> bool:
    """True when the target is a key inside a larger file (e.g. system_prompt)."""
    return isinstance(spec, dict) and "key" in spec


def _target_bytes(spec):
    """Raw bytes of a target file, or None when missing."""
    p = _target_path(spec)
    if not p.exists():
        return None
    return p.read_bytes()


def _target_value(spec):
    """The meaningful value of a target (keyed targets return the key value)."""
    p = _target_path(spec)
    if not p.exists():
        return None
    if _target_keyed(spec):
        try:
            data = config.parse_yaml(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(data, dict):
            return data.get(spec["key"])
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def _write_target(spec, content: str):
    """Write canonical content into a target (keyed targets merge the key)."""
    p = _target_path(spec)
    p.parent.mkdir(parents=True, exist_ok=True)
    if _target_keyed(spec):
        cfg = {}
        if p.exists():
            try:
                cfg = config.parse_yaml(p.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg[spec["key"]] = content
        p.write_text(config.dump_yaml(cfg) + "\n", encoding="utf-8")
    else:
        p.write_text(content, encoding="utf-8")


def _already_matches(spec, content: str) -> bool:
    """True when the target already holds exactly this canonical content.

    Returns False when the target file is missing (the projection should
    create it, not skip it).
    """
    cur = _target_bytes(spec)
    if cur is None:
        return False
    if _target_keyed(spec):
        return _target_value(spec) == content
    return cur == content.encode("utf-8")


def _find_spec_by_target(entry, target: str):
    """(harness, spec) of the mapped target whose file is ``target``, else (None, None).

    Keyed targets match the file path of the mapping; the caller resolves
    conflicts against the key value itself.
    """
    t = str(Path(target))
    for harness, spec in (entry.get("targets") or {}).items():
        if str(_target_path(spec)) == t:
            return harness, spec
    return None, None


# ── written-state (the hash guard's memory) ───────────────────────────────
def _ts() -> str:
    """UTC timestamp with microseconds (unique across rapid saves)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _written_path() -> Path:
    return identity_dir() / WRITTEN_FILE


def _load_written() -> dict:
    """Written-state map: '{name}|{target}' → {hash, ts, owner}."""
    p = _written_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _records_for_path(path: Path) -> list:
    """Written-state records whose target file is ``path`` (any owner)."""
    suffix = f"|{path}"
    return [rec for k, rec in _load_written().items() if k.endswith(suffix)]


def _record_written(key: str, raw: bytes, kind: str = "file"):
    """Remember that Atropos last wrote ``raw`` to the target ``key``.

    ``kind`` is 'file' for whole-file projections and 'keyed' for
    key-inside-file projections (where ``raw`` is the written key value).
    """
    if raw is None:
        return
    state = _load_written()
    state[key] = {"hash": _sha256(raw), "ts": _ts(), "owner": key, "kind": kind}
    p = _written_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _written_bytes_for(spec, content: str) -> bytes:
    """Bytes representing what Atropos just wrote (the guard baseline).

    Whole-file targets record the file's actual bytes; keyed targets
    record the written key's value bytes.
    """
    if _target_keyed(spec):
        return content.encode("utf-8")
    return _target_bytes(spec)


# ── listing / reading ─────────────────────────────────────────────────────
def _excerpt(text: str, limit: int = 120) -> str:
    """First line of text, stripped and truncated."""
    lines = text.splitlines()
    if not lines:
        return ""
    return lines[0].strip()[:limit]


def _file_entry(p: Path, name: str, kind: str) -> dict:
    return {
        "name": name,
        "kind": kind,
        "size": p.stat().st_size,
        "mtime": p.stat().st_mtime,
        "mode": _mode_for(name),
        "consumed_by": list(_targets_for(name).keys()),
        "content_excerpt": _excerpt(p.read_text(encoding="utf-8", errors="replace")),
    }


def list_files() -> list:
    """List every artifact in the canonical store.

    Entries: {name, kind ('identity'|'prompt'), size, mtime, mode,
    consumed_by (harness names the artifact projects to), content_excerpt}.
    """
    root = identity_dir()
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("*.md")):
        out.append(_file_entry(p, p.name, "identity"))
    prompts = root / "prompts"
    if prompts.is_dir():
        for p in sorted(prompts.glob("*.md")):
            out.append(_file_entry(p, p.name, "prompt"))
    return out


def read(name: str) -> str:
    """Canonical content of an artifact (raises FileNotFoundError)."""
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    p = canonical_path(name)
    if not p.exists():
        raise FileNotFoundError(f"identity artifact not found: {name}")
    return p.read_text(encoding="utf-8")


# ── save + projection ─────────────────────────────────────────────────────
def save(name: str, content: str) -> dict:
    """Write an artifact: canonical store first, then shared projections.

    The canonical write always happens and is snapshotted. When the
    artifact's mode is ``shared`` every mapped target is projected with
    the hash guard; drifted targets are reported in ``conflicts`` and
    left untouched. Returns {ok, name, mode, projected, conflicts}.
    """
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    p = canonical_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(name)
    p.write_text(content, encoding="utf-8")
    m = _mode_for(name)
    projected, conflicts = [], []
    if m == "shared":
        for harness, spec in _targets_for(name).items():
            r = _project_one(name, harness, spec, content)
            (projected if "ok" in r else conflicts).append(r)
    return {"ok": True, "name": name, "mode": m,
            "projected": projected, "conflicts": conflicts}


def _project_one(name: str, harness: str, spec, content: str, force: bool = False) -> dict:
    """Project canonical content onto one mapped target (hash-guarded).

    The guard: a target is only overwritten when its current state
    matches what Atropos last wrote there (or it already holds exactly
    the canonical content). Anything else is a conflict.

    The guarded state is the meaningful value: whole-file targets guard
    the file's bytes; keyed targets (e.g. SYSTEM.md → config.yaml's
    ``system_prompt``) guard the key's value, so unrelated edits in the
    surrounding file never trigger a conflict.
    """
    p = _target_path(spec)
    key = f"{name}|{p}"
    kind = "keyed" if _target_keyed(spec) else "file"
    cur = _target_bytes(spec)
    if cur is not None and not force:
        cur_val = cur if kind == "file" else _target_value(spec)
        if cur_val is not None:
            cur_hash = _sha256(cur_val.encode("utf-8") if isinstance(cur_val, str) else cur_val)
            records = _records_for_path(p)
            safe = False
            atropos_hash = None
            for rec in records:
                if (rec.get("owner") == key and rec.get("kind") == kind
                        and rec.get("hash") == cur_hash):
                    safe = True
                if rec.get("owner") == key:
                    atropos_hash = rec.get("hash")
            if not safe and _already_matches(spec, content):
                safe = True
            if not safe:
                return {"conflict": True, "target": str(p), "harness": harness,
                        "local_hash": cur_hash, "atropos_hash": atropos_hash}
    _write_target(spec, content)
    _record_written(key, _written_bytes_for(spec, content), kind=kind)
    return {"target": str(p), "harness": harness, "ok": True}


def resolve_conflict(name: str, target: str, action: str = "overwrite") -> dict:
    """Resolve a projection conflict for one target of ``name``.

    ``action``: 'overwrite' → write the canonical content over the target
    and record the new baseline; 'keep' → adopt the target's current
    content as the acknowledged baseline (nothing is written, so the next
    projection is conflict-free); 'diff' → return the comparison without
    touching anything.
    """
    if action not in ("overwrite", "keep", "diff"):
        raise ValueError(f"invalid action {action!r} — use overwrite | keep | diff")
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    entry = _map_entry(name)
    if not entry:
        raise ValueError(f"no deployment mapping for {name!r}")
    harness, spec = _find_spec_by_target(entry, target)
    if spec is None:
        raise ValueError(f"{target!r} is not a mapped target of {name!r}")
    p = _target_path(spec)
    kind = "keyed" if _target_keyed(spec) else "file"
    content = read(name)
    if action == "diff":
        val = _target_value(spec)
        differs = val != content
        return {"ok": True, "action": "diff", "name": name, "target": str(p),
                "harness": harness, "differs": differs,
                "preview": _diff_preview(content, val)}
    if action == "keep":
        # adopt the target's current state as the acknowledged baseline
        raw = content.encode("utf-8") if kind == "keyed" else _target_bytes(spec)
        if raw is not None:
            _record_written(f"{name}|{p}", raw, kind=kind)
        return {"ok": True, "action": "keep", "name": name,
                "target": str(p), "harness": harness}
    _write_target(spec, content)
    _record_written(f"{name}|{p}", _written_bytes_for(spec, content), kind=kind)
    return {"ok": True, "action": "overwrite", "name": name,
            "target": str(p), "harness": harness}


def sync(name: str) -> dict:
    """Force-project a shared artifact onto all mapped targets (hash-guarded)."""
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    p = canonical_path(name)
    if not p.exists():
        raise FileNotFoundError(f"no canonical copy of {name!r} — save it first")
    m = _mode_for(name)
    if m != "shared":
        return {"ok": True, "name": name, "mode": m, "projected": [],
                "conflicts": [], "note": f"mode is {m} — nothing to project"}
    content = read(name)
    projected, conflicts = [], []
    for harness, spec in _targets_for(name).items():
        r = _project_one(name, harness, spec, content)
        (projected if "ok" in r else conflicts).append(r)
    return {"ok": True, "name": name, "mode": m,
            "projected": projected, "conflicts": conflicts}


# ── modes ─────────────────────────────────────────────────────────────────
def mode(name: str, mode: str) -> dict:
    """Set the deployment mode of one artifact and persist the map.

    Validates the name and the mode ('shared' | 'separate' |
    'atropos-only'). Persisting counts as a user change of
    ``identity.map``, so the merged map is written to settings.
    """
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    if name not in IDENTITY_FILES and name not in PROMPT_FILES:
        raise ValueError(f"unknown identity artifact: {name!r}")
    if mode not in MODES:
        raise ValueError(f"invalid mode {mode!r} — must be one of: {', '.join(MODES)}")
    m = _effective_map()
    entry = m.setdefault(name, {"targets": _default_targets(name), "mode": "shared"})
    entry["mode"] = mode
    _persist_map(m)
    return {"ok": True, "name": name, "mode": mode}


# ── diff ──────────────────────────────────────────────────────────────────
def _diff_preview(a, b, limit: int = 120) -> str:
    """Short human description of the first difference between two texts."""
    if a == b:
        return ""
    if b is None:
        return "(target missing)"
    la, lb = (a or "").splitlines(), (b or "").splitlines()
    for i in range(max(len(la), len(lb))):
        ca = la[i] if i < len(la) else "<eof>"
        cb = lb[i] if i < len(lb) else "<eof>"
        if ca != cb:
            return f"line {i + 1}: canonical={ca[:limit]!r} vs target={cb[:limit]!r}"
    return "(different trailing bytes)"


def diff(name: str) -> dict:
    """Compare the canonical copy against every mapped target.

    Returns {ok, name, diffs: [{target, harness, differs, preview}]}.
    """
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    if not canonical_path(name).exists():
        raise FileNotFoundError(f"no canonical copy of {name!r} — save it first")
    content = read(name)
    out = []
    for harness, spec in _targets_for(name).items():
        val = _target_value(spec)
        differs = val != content
        out.append({"target": str(_target_path(spec)), "harness": harness,
                    "differs": differs,
                    "preview": _diff_preview(content, val) if differs else ""})
    return {"ok": True, "name": name, "diffs": out}


# ── history ───────────────────────────────────────────────────────────────
def _snapshots(name: str) -> list:
    """Snapshot files for an artifact, newest first."""
    hd = history_dir()
    if not hd.exists():
        return []
    return sorted(hd.glob(f"{name}-*"), reverse=True)


def _snapshot(name: str):
    """Copy the current canonical content into .history (prune to 8)."""
    p = canonical_path(name)
    if not p.exists():
        return None
    hd = history_dir()
    hd.mkdir(parents=True, exist_ok=True)
    dest = hd / f"{name}-{_ts()}"
    shutil.copy2(p, dest)
    for old in sorted(hd.glob(f"{name}-*"))[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)
    return dest


def restore(name: str, n: int) -> dict:
    """Restore the n-th most recent snapshot (n=1 = newest) over the canonical copy.

    The pre-restore state is snapshotted first so the rollback is always
    reversible. Only the canonical copy is touched — projections follow
    the caller's explicit save()/sync().
    """
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    snaps = _snapshots(name)
    if not snaps:
        raise FileNotFoundError(f"no snapshots for {name!r}")
    n = int(n)
    if n < 1 or n > len(snaps):
        raise ValueError(f"snapshot index out of range (1..{len(snaps)})")
    _snapshot(name)
    p = canonical_path(name)
    shutil.copy2(snaps[n - 1], p)
    return {"ok": True, "name": name, "restored": snaps[n - 1].name, "from_index": n}


# ── import / detect ───────────────────────────────────────────────────────
def detect_new() -> list:
    """Propose imports of harness identity files not yet registered.

    Scans the Hermes home (SOUL.md, AGENTS.md, SYSTEM*.md, GUEST*.md,
    CODE_STYLE.md), the Claude home (CLAUDE.md, CODE_STYLE.md) and the
    repo root (AGENTS.md). Each entry: {file, name, source, size, mtime}.
    """
    candidates = []
    hermes = detect.hermes_home()
    claude = _claude_home()
    repo = Path(__file__).resolve().parent.parent
    checks = [
        (hermes / "SOUL.md", "SOUL.md", "hermes"),
        (hermes / "AGENTS.md", "AGENTS.md", "hermes"),
        (hermes / "CODE_STYLE.md", "CODE_STYLE.md", "hermes"),
        (claude / "CLAUDE.md", "SOUL.md", "claude"),
        (claude / "CODE_STYLE.md", "CODE_STYLE.md", "claude"),
        (repo / "AGENTS.md", "AGENTS.md", "repo"),
    ]
    if hermes.exists():
        for p in sorted(hermes.glob("SYSTEM*.md")):
            checks.append((p, "SYSTEM.md", "hermes"))
        for p in sorted(hermes.glob("GUEST*.md")):
            checks.append((p, "GUEST.md", "hermes"))
    seen = set()
    for p, name, source in checks:
        if not p.is_file() or (str(p), name) in seen:
            continue
        seen.add((str(p), name))
        if canonical_path(name).exists():
            continue
        candidates.append({
            "file": str(p), "name": name, "source": source,
            "size": p.stat().st_size, "mtime": p.stat().st_mtime,
        })
    return candidates


def import_file(name: str, source_path, mode: str = "shared") -> dict:
    """Import a harness identity file into the canonical store.

    Copies ``source_path`` into ``identity/<name>`` (or ``prompts/`` for
    prompt artifacts), registers the deployment mapping (default targets,
    requested mode) and persists it. Raises ValueError when the artifact
    is already registered or the mode is invalid; FileNotFoundError when
    the source is missing.
    """
    if not valid_name(name):
        raise ValueError(f"invalid artifact name: {name!r}")
    if mode not in MODES:
        raise ValueError(f"invalid mode {mode!r} — must be one of: {', '.join(MODES)}")
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(f"source file not found: {source_path}")
    p = canonical_path(name)
    if p.exists():
        raise ValueError(f"{name!r} is already registered — use save() to update it")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src.read_bytes())
    m = _effective_map()
    m[name] = {"targets": _default_targets(name), "mode": mode}
    _persist_map(m)
    return {"ok": True, "name": name, "mode": mode, "from": str(src),
            "size": p.stat().st_size}


# ── stats ─────────────────────────────────────────────────────────────────
def stats() -> dict:
    """Store overview: {files, total_bytes, by_mode}."""
    files = list_files()
    by_mode = {}
    for f in files:
        by_mode[f["mode"]] = by_mode.get(f["mode"], 0) + 1
    return {"files": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "by_mode": by_mode}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="universal identity manager")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["list", "read", "save", "mode", "sync", "diff",
                             "restore", "stats", "detect"])
    ap.add_argument("args", nargs="*")
    args = ap.parse_args()
    try:
        if args.cmd == "list":
            for f in list_files():
                print(f"  {f['name']:<28} {f['mode']:<12} {f['size']:>6} B  by: {', '.join(f['consumed_by']) or '-'}")
        elif args.cmd == "read":
            print(read(args.args[0]))
        elif args.cmd == "save":
            name, path = args.args[0], args.args[1]
            print(json.dumps(save(name, Path(path).read_text(encoding="utf-8")), indent=2))
        elif args.cmd == "mode":
            print(json.dumps(mode(args.args[0], args.args[1]), indent=2))
        elif args.cmd == "sync":
            print(json.dumps(sync(args.args[0]), indent=2))
        elif args.cmd == "diff":
            print(json.dumps(diff(args.args[0]), indent=2))
        elif args.cmd == "restore":
            print(json.dumps(restore(args.args[0], int(args.args[1])), indent=2))
        elif args.cmd == "stats":
            print(json.dumps(stats(), indent=2))
        elif args.cmd == "detect":
            for c in detect_new():
                print(f"  {c['source']:<7} {c['name']:<12} {c['file']} ({c['size']} B)")
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
