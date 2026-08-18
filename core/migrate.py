#!/usr/bin/env python3
"""Atropos migration import — bring external Hermes state into Atropos.

Ask-first, revertible. Nothing is written without ``yes=True``, and every
import snapshots the pre-import state into ``~/.atropos/backups/migrate_<ts>/``
plus a ``migrations.jsonl`` log line (the shared log format referenced by
core/autoskill.py ``_MIGRATIONS``). ``undo()`` restores the last snapshot.

Imports (all optional per-kind, default everything available):
  * config   — merge known settings keys from an external config.yaml
  * memory   — copy non-duplicate memory notes (memory.json)
  * skills   — copy skill dirs (skip existing names, unless ``replace=True``)

The source is ``$HERMES_HOME`` by default (or an explicit path). Reading
never mutates the source — only the target Atropos home is written.
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import detect, settings

_MIGRATIONS = "migrations.jsonl"
_BACKUP_DIR = "backups/migrate_"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_line(**extra) -> dict:
    """Append one line to ~/.atropos/migrations.jsonl; returns the record."""
    rec = {"ts": _now(), "action": extra.pop("action", "import"), **extra}
    p = detect.atropos_home() / _MIGRATIONS
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return rec


def history(limit: int = 50) -> list:
    """Recent migration records (newest first)."""
    p = detect.atropos_home() / _MIGRATIONS
    if not p.exists():
        return []
    rows = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows[-limit:][::-1]


# ── plan (ask-first: dry-run) ────────────────────────────────────────────
def import_plan(source=None, kinds=None) -> dict:
    """What importing would touch. No writes.

    ``source`` defaults to $HERMES_HOME. ``kinds`` = subset of
    config|memory|skills (default: all present in the source).
    """
    src = Path(source) if source else detect.hermes_home()
    plan = {"source": str(src), "exists": src.exists(), "kinds": {}}
    if not src.exists():
        return plan
    want = kinds or ["config", "memory", "skills"]

    if "config" in want:
        cfg = src / "config.yaml"
        plan["kinds"]["config"] = {"found": cfg.exists()}
        if cfg.exists():
            raw = _read_yaml_loose(cfg)
            keys = sorted(k for k in _iter_known_keys(raw))
            plan["kinds"]["config"]["known_keys"] = keys
            plan["kinds"]["config"]["unknown_keys"] = sorted(
                k for k in raw if not _known_prefix(k) and k not in ("version", "hermes", "claude"))

    if "memory" in want:
        mem = src / "memory.json"
        plan["kinds"]["memory"] = {"found": mem.exists()}
        if mem.exists():
            notes = _read_json_list(mem)
            plan["kinds"]["memory"]["notes"] = len(notes)
            plan["kinds"]["memory"]["duplicates"] = _prune_existing(notes)["dropped"]

    if "skills" in want:
        sd = src / "skills"
        if sd.exists():
            dirs = sorted(d.name for d in sd.iterdir()
                          if d.is_dir() and (d / "SKILL.md").exists())
        else:
            dirs = []
        plan["kinds"]["skills"] = {
            "found": sd.exists(),
            "skills": dirs,
            "already_present": sorted(n for n in dirs
                                      if (detect.atropos_home() / "skills" / n).exists()),
        }
    return plan


# ── apply (writes; backup first) ─────────────────────────────────────────
def import_apply(source=None, kinds=None, yes=False, replace=False) -> dict:
    """Perform the import. Requires ``yes=True`` (ask-first contract).

    Steps: snapshot target state → copy each chosen kind → log. Returns
    {ok, imported: {...}, backup}. Undo-able via undo().
    """
    if not yes:
        return {"ok": False, "reason": "ask-first: pass yes=True (dry-run available via import_plan)"}
    src = Path(source) if source else detect.hermes_home()
    if not src.exists():
        return {"ok": False, "reason": f"source not found: {src}"}
    home = detect.atropos_home()
    backup = home / (_BACKUP_DIR + _now().replace(":", "-"))
    backup.mkdir(parents=True, exist_ok=True)

    want = kinds or ["config", "memory", "skills"]
    imported = {"config": [], "memory": 0, "skills": []}
    if "config" in want:
        cfg = src / "config.yaml"
        if cfg.exists():
            raw = _read_yaml_loose(cfg)
            _snap(home / "config.yaml", backup, "config")
            merged = _merge_known(raw)
            settings.migrate()  # ensure schema keys exist
            imported["config"] = merged

    if "memory" in want:
        mem = src / "memory.json"
        if mem.exists():
            _snap(home / "memory.json", backup, "memory")
            target = home / "memory.json"
            existing = _read_json_list(target)
            existing_texts = {str(n.get("text", "")) for n in existing}
            combined = existing.copy()
            for n in _read_json_list(mem):
                if str(n.get("text", "")) in existing_texts:
                    continue
                combined.append(n)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(combined, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            imported["memory"] = len(combined) - len(existing)

    if "skills" in want:
        sd = src / "skills"
        if sd.exists():
            import core.skills as _sk
            tgt_root = _sk.skills_dir()
            for d in sorted(sd.iterdir()):
                if not d.is_dir() or not (d / "SKILL.md").exists():
                    continue
                target = tgt_root / d.name
                if target.exists():
                    if replace:
                        _snap(target, backup, f"skills/{d.name}")
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        continue
                shutil.copytree(d, target,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                imported["skills"].append(d.name)

    record = _log_line(action="import", source=str(src), kinds=want,
                       imported=imported, backup=str(backup))
    return {"ok": True, "imported": imported, "backup": str(backup),
            "record": record}


# ── undo (revert last import) ────────────────────────────────────────────
def undo(yes=False) -> dict:
    """Revert the most recent import from its snapshot backup.

    Restores config.yaml if it was imported, drops memory notes added by the
    import (the snapshot restores the whole file), and removes imported
    skills. Requires ``yes=True``.
    """
    if not yes:
        return {"ok": False, "reason": "ask-first: pass yes=True"}
    rows = history(1)
    if not rows:
        return {"ok": False, "reason": "no prior imports"}
    rec = rows[0]
    backup = Path(rec.get("backup", ""))
    home = detect.atropos_home()
    restored = []
    if backup.exists():
        for f in ("config.yaml", "memory.json"):
            snap = backup / f
            if snap.exists():
                shutil.copy2(snap, home / f)
                restored.append(f)
    imported = rec.get("imported", {})
    removed = []
    for name in (imported.get("skills") or []):
        target = home / "skills" / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
    _log_line(action="undo", restored=restored, removed_skills=removed,
              of=rec.get("ts", ""))
    return {"ok": True, "restored": restored, "removed_skills": removed}


# ── helpers ──────────────────────────────────────────────────────────────
def _read_yaml_loose(path: Path) -> dict:
    """Parse config.yaml without a YAML lib: top-level key: value lines
    (nested blocks become dict-of-strings; _iter_known_keys flattens what it
    can)."""
    out = {}
    cur = None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.endswith(":") and not s.startswith(("-", " ")):
                cur = s[:-1]
                out.setdefault(cur, {})
                continue
            if ":" in s and not s.startswith("-"):
                k, _, v = s.partition(":")
                if cur and not line.startswith(" "):
                    out[k.strip()] = v.strip()
                    cur = None
                elif cur:
                    try:
                        out[cur][k.strip()] = _loose_value(v.strip())
                    except Exception:
                        pass
                else:
                    out[k.strip()] = _loose_value(v.strip())
    except OSError:
        pass
    return out


def _loose_value(v: str):
    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _known_prefix(k: str) -> bool:
    return any(k == p or k.startswith(p + ".") for p in
               tuple(x.split(".")[0] for x in settings.SETTINGS_SCHEMA))


def _iter_known_keys(raw: dict):
    """Yield known settings keys present in the loose parse (flattened)."""
    for key, spec in settings.SETTINGS_SCHEMA.items():
        if spec.get("readonly"):
            continue
        parts = key.split(".")
        node = raw
        ok = True
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                ok = False
                break
            node = node[p]
        if ok:
            yield key


def _merge_known(raw: dict) -> list:
    """Set every known key present in the source config (best-effort)."""
    merged = []
    for key in _iter_known_keys(raw):
        parts = key.split(".")
        node = raw
        for p in parts:
            node = node[p]
        try:
            settings.set(key, node)
            merged.append(key)
        except (ValueError, TypeError):
            continue
    return merged


def _read_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        for candidate in ("notes", "items", "memory"):
            if isinstance(data.get(candidate), list):
                return data[candidate]
        return []
    return data if isinstance(data, list) else []


def _prune_existing(notes: list) -> dict:
    """Notes from the source that would NOT be duplicates at the target."""
    target = detect.atropos_home() / "memory.json"
    existing = _read_json_list(target)
    existing_texts = {str(n.get("text", "")) for n in existing}
    added = [n for n in notes if str(n.get("text", "")) not in existing_texts]
    return {"added": added, "dropped": len(notes) - len(added)}


def _snap(path: Path, backup: Path, name: str):
    """Snapshot one target file/dir into the backup dir.

    ``name`` is the logical resource ("config", "memory", "skills/<name>");
    the on-disk entry keeps the resource's real basename so undo can find
    it by filename."""
    if path.exists():
        dst = backup / (path.name if path.is_file() else name.replace("/", "__"))
        if path.is_dir():
            shutil.copytree(path, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)


if __name__ == "__main__":
    mode = sys.argv[1] if sys.argv[1:] else "plan"
    if mode == "plan":
        print(json.dumps(import_plan(), indent=2))
    elif mode == "apply":
        print(json.dumps(import_apply(yes="--yes" in sys.argv), indent=2))
    elif mode == "undo":
        print(json.dumps(undo(yes="--yes" in sys.argv), indent=2))
    else:
        print("usage: python -m core.migrate plan|apply [--yes]|undo [--yes]")