#!/usr/bin/env python3
"""Atropos snapshots — point-in-time config gallery, stdlib only.

A snapshot tarballs config.yaml + the identity/ directory + the current
settings export into detect.atropos_home()/snapshots/<ts>-<slug>.tar.gz.
Restore extracts config.yaml back through a staging file, then swaps it
into place atomically; identity/ and settings.yaml restore as
``<file>.restore`` copies so nothing is ever clobbered by a snapshot.
"""
import json
import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, settings


def snapshots_dir() -> Path:
    """Directory where snapshot tarballs are stored."""
    return detect.atropos_home() / "snapshots"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _slug(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or "snapshot").strip()).strip("-")
    return (slug or "snapshot")[:40]


def _identity_dir() -> Path:
    # ~/.atropos/identity is the canonical location for universal identity files
    return detect.atropos_home() / "identity"


def create(label: str = "snapshot") -> dict:
    """Create a snapshot tarball. Returns {ok, name, path, size, ts, label}.

    Contents: config.yaml, identity/ (files or empty dir), and
    settings.yaml (the schema-normalized settings export, secrets masked).
    """
    ts = _ts()
    snap_dir = snapshots_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    name = f"{ts}-{_slug(label)}.tar.gz"
    dest = snap_dir / name

    items = []
    cfg = config.config_path()
    if cfg.exists():
        items.append(("config.yaml", cfg))
    ident = _identity_dir()
    if ident.exists():
        items.append(("identity", ident))
    elif snap_dir.exists():
        ident.mkdir(parents=True, exist_ok=True)
        items.append(("identity", ident))

    with tarfile.open(dest, "w:gz") as tar:
        for arcname, src in items:
            tar.add(src, arcname=arcname, recursive=True, filter=lambda t: (
                None if "__pycache__" in t.name or t.name.endswith(".pyc") else t
            ))
        try:
            data = settings.export_yaml(include_secrets=False)
        except Exception:
            data = ""
        if data:
            import io
            info = tarfile.TarInfo("settings.yaml")
            blob = data.encode("utf-8")
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))

    return {
        "ok": True,
        "name": name,
        "path": str(dest),
        "size": dest.stat().st_size,
        "ts": ts,
        "label": label,
    }


def list_snapshots() -> list:
    """List snapshots newest-first.

    Each row: {name, label, size, ts, created}. Label is parsed from the
    file name (``<ts>-<slug>.tar.gz``); ts is the file mtime in ISO form.
    """
    snap_dir = snapshots_dir()
    if not snap_dir.exists():
        return []
    out = []
    for f in sorted(snap_dir.glob("*.tar.gz"), reverse=True):
        stem = f.name[: -len(".tar.gz")]
        # stem shape: YYYYMMDD_HHMMSS-<slug> (underscore at index 8)
        ts = stem[:15] if len(stem) >= 15 and stem[8] == "_" else ""
        label = stem[16:] if ts else stem
        out.append({
            "name": f.name,
            "label": label,
            "size": f.stat().st_size,
            "ts": ts,
            "created": f.stat().st_mtime,
        })
    return out


def restore(name: str) -> dict:
    """Restore a snapshot. Returns {ok, restored: [...], from: name}.

    config.yaml is extracted to ``config.yaml.restore`` inside the staging
    area and moved over the live file. identity/ and settings.yaml are
    written back as ``<file>.restore`` copies (never overwriting live
    files). Missing files are skipped, not fatal.
    """
    src = snapshots_dir() / name
    if not src.exists():
        return {"ok": False, "error": f"snapshot not found: {name}"}

    restored = []
    errors = []
    staging = snapshots_dir() / ".restore"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(src, "r:gz") as tar:
            for member in tar.getmembers():
                if member.isdir():
                    continue
                extract = tar.extractfile(member)
                if extract is None:
                    continue
                payload = extract.read()
                fname = Path(member.name).name
                if member.name == "config.yaml":
                    stage = staging / "config.yaml.restore"
                    stage.write_bytes(payload)
                    config.config_path().parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(stage), str(config.config_path()))
                    restored.append("config.yaml")
                elif member.name.startswith("identity/"):
                    rel = Path(member.name).relative_to("identity")
                    if not rel.name:
                        continue
                    ident = _identity_dir()
                    ident.mkdir(parents=True, exist_ok=True)
                    (ident / f"{rel.name}.restore").write_bytes(payload)
                    restored.append(str(rel))
                elif member.name == "settings.yaml":
                    stage = staging / "settings.yaml.restore"
                    stage.write_bytes(payload)
                    target = detect.atropos_home() / "settings.yaml.restore"
                    shutil.move(str(stage), str(target))
                    restored.append("settings.yaml")
    except Exception as e:
        errors.append(str(e))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if errors and not restored:
        return {"ok": False, "error": "; ".join(errors)}
    result = {"ok": True, "restored": restored, "from": name}
    if errors:
        result["errors"] = errors
    return result


def prune(keep: int = 10) -> list:
    """Delete old snapshots beyond ``keep``. Returns removed names."""
    keep = max(0, int(keep))
    removed = []
    for snap in list_snapshots()[keep:]:
        try:
            (snapshots_dir() / snap["name"]).unlink()
            removed.append(snap["name"])
        except Exception:
            pass
    return removed


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "create":
        print(json.dumps(create(sys.argv[2] if len(sys.argv) > 2 else "snapshot"),
                         indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "restore":
        print(json.dumps(restore(sys.argv[2]), indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "prune":
        print(json.dumps(prune(int(sys.argv[2]) if len(sys.argv) > 2 else 10),
                         indent=2, ensure_ascii=False))
    else:
        for snap in list_snapshots():
            print(snap)
