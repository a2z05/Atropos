#!/usr/bin/env python3
"""Atropos conflayer — universal config manager.

Mirrors every harness config file into one canonical store,
``~/.atropos/configs/``:

  * ``hermes.yaml``         copy of the Hermes config.yaml
  * ``hermes.env``          Hermes .env (key=value lines)
  * ``claude.settings.json`` copy of ~/.claude/settings.json
  * ``claude.mcp.json``     copy of ~/.claude/mcp.json
  * ``router.yaml``         the router section of the Atropos config
  * ``atropos.yaml``        the Atropos config.yaml

The canonical copy is the edit surface; the live path is only touched on
explicit save()/sync() when the file's mode is ``shared``. Modes (from
settings ``configs.mode``, default ``separate``):

  * ``shared``       — canonical is authoritative; live path is projected
                       on save()/sync() (hash-guarded, never silent).
  * ``separate``     — harnesses keep their own copies; Atropos mirrors
                       and monitors only.
  * ``atropos-only`` — canonical lives only in Atropos; never projected.

Per-file mode overrides persist in ``configs/.modes.json``; unlisted
files fall back to the settings default.

CONFLICT POLICY (the universal rule): a projection never silently
overwrites a live file that differs from the last content Atropos wrote.
On drift, save()/sync() return a conflict dict and leave the live file
untouched; resolve_conflict(name, target, action) settles it with
``overwrite`` | ``keep`` | ``diff``.

save() parse-validates before writing (bad JSON / bad .env lines are
rejected outright; YAML is parsed with the project's config parser).
Every save snapshots the previous canonical content into
``configs/.history/<name>-<ts>`` (last 8 kept); rollback() restores one.
"""
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, settings

MODES = ("shared", "separate", "atropos-only")
CONFIG_NAMES = ("hermes.yaml", "hermes.env", "claude.settings.json",
                "claude.mcp.json", "router.yaml", "atropos.yaml")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HISTORY_KEEP = 8
MODES_FILE = ".modes.json"
WRITTEN_FILE = ".written.json"


# ── paths ─────────────────────────────────────────────────────────────────
def configs_dir() -> Path:
    """Canonical mirror store (~/.atropos/configs)."""
    return detect.atropos_home() / "configs"


def history_dir() -> Path:
    """Snapshot history subdir of the mirror store."""
    return configs_dir() / ".history"


def canonical_path(name: str) -> Path:
    """Canonical location of one mirrored config."""
    return configs_dir() / name


def valid_name(name: str) -> bool:
    """True when ``name`` is a known mirrored config (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name) and name in CONFIG_NAMES)


def _live_spec(name: str):
    """Live-path spec of a config: a path string or {path, key} for the
    router section, which lives as a key inside the Atropos config."""
    if name == "hermes.yaml":
        return str(detect.hermes_home() / "config.yaml")
    if name == "hermes.env":
        return str(detect.hermes_home() / ".env")
    if name == "claude.settings.json":
        return str(detect._home() / ".claude" / "settings.json")
    if name == "claude.mcp.json":
        return str(detect._home() / ".claude" / "mcp.json")
    if name == "router.yaml":
        return {"path": str(config.config_path()), "key": "router"}
    return str(config.config_path())


def live_path(name: str) -> Path:
    """Filesystem path of the live file a config projects to."""
    spec = _live_spec(name)
    return Path(spec["path"]) if isinstance(spec, dict) else Path(spec)


def _target_keyed(spec) -> bool:
    """True when the live target is a key inside a larger file (router.yaml)."""
    return isinstance(spec, dict) and "key" in spec


def _target_bytes(spec):
    """Raw bytes of a live file, or None when missing."""
    p = Path(spec["path"]) if isinstance(spec, dict) else Path(spec)
    if not p.exists():
        return None
    return p.read_bytes()


def _write_target(spec, content: str):
    """Write content into the live file (keyed targets merge the key)."""
    p = Path(spec["path"]) if isinstance(spec, dict) else Path(spec)
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
        cfg[spec["key"]] = config.parse_yaml(content) if content.strip() else {}
        p.write_text(config.dump_yaml(cfg) + "\n", encoding="utf-8")
    else:
        p.write_text(content, encoding="utf-8")


def _already_matches(spec, content: str) -> bool:
    """True when the live file already holds exactly this canonical content."""
    if _target_keyed(spec):
        p = Path(spec["path"])
        if not p.exists():
            return False
        try:
            cfg = config.parse_yaml(p.read_text(encoding="utf-8"))
        except Exception:
            return False
        parsed = config.parse_yaml(content) if content.strip() else {}
        return isinstance(cfg, dict) and cfg.get(spec["key"]) == parsed
    cur = _target_bytes(spec)
    return cur is not None and cur == content.encode("utf-8")


# ── per-file modes ────────────────────────────────────────────────────────
def _modes_file() -> Path:
    return configs_dir() / MODES_FILE


def _load_modes() -> dict:
    """Per-file mode overrides from configs/.modes.json."""
    p = _modes_file()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _mode_for(name: str) -> str:
    """Effective mode of one config: per-file override, else settings default."""
    overrides = _load_modes()
    if name in overrides and overrides[name] in MODES:
        return overrides[name]
    m = settings.get("configs.mode", "separate")
    return m if m in MODES else "separate"


def mode(name: str, mode: str) -> dict:
    """Set the deployment mode of one config and persist the override.

    Validates both the name and the mode ('shared' | 'separate' |
    'atropos-only'). The override lives in configs/.modes.json; the
    settings ``configs.mode`` default keeps applying to every other file.
    """
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    if mode not in MODES:
        raise ValueError(f"invalid mode {mode!r} — must be one of: {', '.join(MODES)}")
    overrides = _load_modes()
    overrides[name] = mode
    p = _modes_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    return {"ok": True, "name": name, "mode": mode}


# ── written-state (the hash guard's memory) ───────────────────────────────
def _ts() -> str:
    """UTC timestamp with microseconds (unique across rapid saves)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _written_path() -> Path:
    return configs_dir() / WRITTEN_FILE


def _load_written() -> dict:
    """Written-state map: '{name}|{live_path}' → {hash, ts, owner}."""
    p = _written_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_written(key: str, raw: bytes, kind: str = "file"):
    """Remember that Atropos last wrote ``raw`` to the live path ``key``.

    ``kind`` is 'file' for whole-file projections (hash over the file
    bytes) or 'json' for JSON configs (hash over the semantic value, so
    whitespace-only edits by the harness never trigger a conflict).
    """
    if raw is None:
        return
    state = _load_written()
    state[key] = {"hash": _sha256(raw), "ts": _ts(), "owner": key, "kind": kind}
    p = _written_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _written_bytes_for(name: str, content: str) -> bytes:
    """Bytes representing what Atropos just wrote (the guard baseline).

    JSON configs record the semantic value (so future writes of the same
    content never self-conflict regardless of formatting); everything
    else records the live file's actual bytes.
    """
    if name.endswith(".json"):
        try:
            return json.dumps(json.loads(content), sort_keys=True).encode("utf-8")
        except Exception:
            return content.encode("utf-8")
    return _target_bytes(_live_spec(name))


def _project_live(name: str, content: str, force: bool = False) -> dict:
    """Project canonical content onto the live path (hash-guarded).

    A live file is only overwritten when its current state matches what
    Atropos last wrote there (or it already holds exactly the canonical
    content). Anything else is a conflict. JSON configs are compared
    semantically (parsed values), so formatting-only differences are not
    conflicts. An existing live file that Atropos has never written and
    that differs from the canonical content is a conflict, never a
    silent overwrite.
    """
    spec = _live_spec(name)
    p = live_path(name)
    key = f"{name}|{p}"
    kind = "json" if name.endswith(".json") else "file"
    cur = _target_bytes(spec)
    if cur is not None and not force:
        try:
            if kind == "json":
                cur_hash = _sha256(
                    json.dumps(json.loads(cur), sort_keys=True).encode("utf-8"))
            else:
                cur_hash = _sha256(cur)
        except Exception:
            # unparsable live file — it is not what Atropos wrote, so it
            # can never match the baseline: treat as a conflict
            return {"conflict": True, "target": str(p),
                    "local_hash": None, "atropos_hash": None}
        state = _load_written()
        safe = False
        atropos_hash = None
        for rec in state.values():
            if (str(rec.get("path", "")) == str(p) and rec.get("owner") == key
                    and rec.get("kind") == kind and rec.get("hash") == cur_hash):
                safe = True
            if rec.get("owner") == key:
                atropos_hash = rec.get("hash")
        if not safe and _already_matches(spec, content):
            safe = True
        if not safe:
            return {"conflict": True, "target": str(p),
                    "local_hash": cur_hash, "atropos_hash": atropos_hash}
    _write_target(spec, content)
    _record_written(key, _written_bytes_for(name, content), kind=kind)
    return {"target": str(p), "ok": True}


# ── listing / showing ─────────────────────────────────────────────────────
def list_configs() -> list:
    """Every mirrored config: {name, path, live_path, exists, size, mode}."""
    out = []
    for name in CONFIG_NAMES:
        p = canonical_path(name)
        out.append({
            "name": name,
            "path": str(p),
            "live_path": str(live_path(name)),
            "exists": p.exists(),
            "size": p.stat().st_size if p.exists() else 0,
            "mode": _mode_for(name),
        })
    return out


def show(name: str) -> dict:
    """Canonical content of one config with metadata.

    Returns {ok, name, path, live_path, exists, mode, size, content}.
    """
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    p = canonical_path(name)
    return {
        "ok": True, "name": name,
        "path": str(p), "live_path": str(live_path(name)),
        "exists": p.exists(), "mode": _mode_for(name),
        "size": p.stat().st_size if p.exists() else 0,
        "content": p.read_text(encoding="utf-8", errors="replace") if p.exists() else "",
    }


# ── validation ────────────────────────────────────────────────────────────
def _validate_yaml(text: str) -> tuple:
    """Parse with the project YAML parser (lenient subset)."""
    data = config.parse_yaml(text)
    return True, [], {"type": "yaml", "keys": len(data) if isinstance(data, dict) else 0}


def _validate_json(text: str) -> tuple:
    """Strict JSON parse; errors carry line/col from the decoder."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return False, [{"line": e.lineno, "col": e.colno, "msg": str(e)}], {}
    return True, [], {"type": "json", "keys": len(data) if isinstance(data, dict) else 0}


def _validate_env(text: str) -> tuple:
    """key=value line validation for .env files."""
    errors = []
    keys = 0
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append({"line": i, "col": 1, "msg": "expected key=value"})
            continue
        k = line.partition("=")[0].strip()
        if not ENV_KEY_RE.match(k):
            errors.append({"line": i, "col": 1, "msg": f"invalid env key: {k!r}"})
            continue
        keys += 1
    return not errors, errors, {"type": "env", "keys": keys}


def validate(name: str, content: str = None) -> dict:
    """Parse-validate one config.

    ``content`` defaults to the canonical copy when omitted (missing file
    is a validation error). Returns
    {ok, errors: [{line, col, msg}], summary: {type, keys}}.
    """
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    if content is None:
        p = canonical_path(name)
        if not p.exists():
            return {"ok": False, "errors": [{"line": 1, "col": 1,
                                             "msg": "no canonical copy yet"}],
                    "summary": {}}
        content = p.read_text(encoding="utf-8", errors="replace")
    if name.endswith(".json"):
        ok, errors, summary = _validate_json(content)
    elif name.endswith(".env"):
        ok, errors, summary = _validate_env(content)
    else:
        ok, errors, summary = _validate_yaml(content)
    return {"ok": ok, "errors": errors, "summary": summary}


# ── save + projection ─────────────────────────────────────────────────────
def save(name: str, content: str) -> dict:
    """Validate + write a mirrored config.

    Invalid content (bad JSON, malformed .env lines) raises ValueError
    with the first error's line/col. Valid content is snapshotted, then
    written to the canonical store; when the mode is ``shared`` it is
    also projected to the live path with the hash guard. Returns
    {ok, name, mode, written, conflicts}.
    """
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    result = validate(name, content)
    if not result["ok"]:
        first = result["errors"][0]
        raise ValueError(
            f"invalid {name}: line {first['line']} col {first['col']}: {first['msg']}")
    p = canonical_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(name)
    p.write_text(content, encoding="utf-8")
    m = _mode_for(name)
    written, conflicts = [], []
    if m == "shared":
        r = _project_live(name, content)
        (written if "ok" in r else conflicts).append(r)
    return {"ok": True, "name": name, "mode": m,
            "written": written, "conflicts": conflicts}


def sync(name: str) -> dict:
    """Force-project a shared config onto its live path (hash-guarded)."""
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    p = canonical_path(name)
    if not p.exists():
        raise FileNotFoundError(f"no canonical copy of {name!r} — save it first")
    m = _mode_for(name)
    if m != "shared":
        return {"ok": True, "name": name, "mode": m, "written": [],
                "conflicts": [], "note": f"mode is {m} — nothing to project"}
    content = p.read_text(encoding="utf-8")
    r = _project_live(name, content)
    written = [r] if "ok" in r else []
    conflicts = [] if "ok" in r else [r]
    return {"ok": True, "name": name, "mode": m,
            "written": written, "conflicts": conflicts}


def resolve_conflict(name: str, target: str, action: str = "overwrite") -> dict:
    """Resolve a projection conflict for one config's live path.

    ``action``: 'overwrite' → write the canonical content over the live
    file and record the new baseline; 'keep' → adopt the live file's
    current content as the acknowledged baseline (nothing written, the
    next projection is conflict-free); 'diff' → compare without touching.
    """
    if action not in ("overwrite", "keep", "diff"):
        raise ValueError(f"invalid action {action!r} — use overwrite | keep | diff")
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    live = live_path(name)
    if str(Path(target)) != str(live):
        raise ValueError(f"{target!r} is not the live path of {name!r} ({live})")
    spec = _live_spec(name)
    kind = "json" if name.endswith(".json") else "file"
    content = canonical_path(name).read_text(encoding="utf-8", errors="replace")
    if action == "diff":
        cur = _target_bytes(spec)
        differs = cur is None or cur.decode("utf-8", errors="replace") != content
        return {"ok": True, "action": "diff", "name": name, "target": str(live),
                "differs": differs, "preview": _diff_preview(content, cur)}
    if action == "keep":
        raw = _written_bytes_for(name, content) if kind == "json" else _target_bytes(spec)
        if raw is not None:
            _record_written(f"{name}|{live}", raw, kind=kind)
        return {"ok": True, "action": "keep", "name": name, "target": str(live)}
    _write_target(spec, content)
    _record_written(f"{name}|{live}", _written_bytes_for(name, content), kind=kind)
    return {"ok": True, "action": "overwrite", "name": name, "target": str(live)}


# ── diff ──────────────────────────────────────────────────────────────────
def _diff_preview(a: str, b, limit: int = 120) -> str:
    """Short human description of the first difference, or '' when equal."""
    if b is None:
        return "(live file missing)"
    b = b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b
    if a == b:
        return ""
    la, lb = a.splitlines(), b.splitlines()
    for i in range(max(len(la), len(lb))):
        ca = la[i] if i < len(la) else "<eof>"
        cb = lb[i] if i < len(lb) else "<eof>"
        if ca != cb:
            return f"line {i + 1}: canonical={ca[:limit]!r} vs live={cb[:limit]!r}"
    return "(different trailing bytes)"


def diff(name: str) -> dict:
    """Compare the canonical copy against the live path.

    Returns {ok, name, target, differs, preview}.
    """
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
    p = canonical_path(name)
    if not p.exists():
        raise FileNotFoundError(f"no canonical copy of {name!r} — save it first")
    content = p.read_text(encoding="utf-8", errors="replace")
    cur = _target_bytes(_live_spec(name))
    differs = cur is None or cur.decode("utf-8", errors="replace") != content
    return {"ok": True, "name": name, "target": str(live_path(name)),
            "differs": differs, "preview": _diff_preview(content, cur) if differs else ""}


# ── snapshots ─────────────────────────────────────────────────────────────
def _snapshots(name: str) -> list:
    """Snapshot files for one config, newest first."""
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


def rollback(name: str, n: int) -> dict:
    """Restore the n-th most recent snapshot (n=1 = newest) over the canonical copy.

    The pre-rollback state is snapshotted first so the rollback is always
    reversible. Only the canonical copy is touched — the live path only
    changes through an explicit save()/sync().
    """
    if not valid_name(name):
        raise ValueError(f"unknown config: {name!r}")
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


# ── stats ─────────────────────────────────────────────────────────────────
def stats() -> dict:
    """Store overview: {configs, total_bytes, by_mode}."""
    configs = list_configs()
    by_mode = {}
    for c in configs:
        by_mode[c["mode"]] = by_mode.get(c["mode"], 0) + 1
    return {"configs": len(configs),
            "total_bytes": sum(c["size"] for c in configs),
            "by_mode": by_mode}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="universal config manager")
    ap.add_argument("cmd", nargs="?", default="list",
                    choices=["list", "show", "validate", "save", "mode",
                             "sync", "diff", "rollback", "stats"])
    ap.add_argument("args", nargs="*")
    args = ap.parse_args()
    try:
        if args.cmd == "list":
            for c in list_configs():
                print(f"  {c['name']:<22} {c['mode']:<12} {'y' if c['exists'] else '-'}  {c['size']:>6} B  → {c['live_path']}")
        elif args.cmd == "show":
            print(json.dumps(show(args.args[0]), indent=2, ensure_ascii=False))
        elif args.cmd == "validate":
            print(json.dumps(validate(args.args[0]), indent=2, ensure_ascii=False))
        elif args.cmd == "save":
            name, path = args.args[0], args.args[1]
            print(json.dumps(save(name, Path(path).read_text(encoding="utf-8")), indent=2))
        elif args.cmd == "mode":
            print(json.dumps(mode(args.args[0], args.args[1]), indent=2))
        elif args.cmd == "sync":
            print(json.dumps(sync(args.args[0]), indent=2))
        elif args.cmd == "diff":
            print(json.dumps(diff(args.args[0]), indent=2))
        elif args.cmd == "rollback":
            print(json.dumps(rollback(args.args[0], int(args.args[1])), indent=2))
        elif args.cmd == "stats":
            print(json.dumps(stats(), indent=2))
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
