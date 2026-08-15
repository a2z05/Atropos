#!/usr/bin/env python3
"""Atropos backup — full state backup with rotation.

Backs up: config, hacks, patches/, state.db (compressed), sessions dir,
scripts, hooks — everything needed to restore after a wipe.

Usage:
  atropos backup                  # create a backup
  atropos backup --list           # list existing backups
  atropos backup --restore <name> # restore from backup
  atropos backup --prune N        # keep only N most recent
"""
import argparse
import gzip
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, settings

MAX_BACKUPS = 5  # legacy constant; retention now reads settings.backup.retention


def _retention() -> int:
    """Backup retention count from settings (default 5, min 1)."""
    try:
        return max(1, int(settings.get("backup.retention", MAX_BACKUPS)))
    except Exception:
        return MAX_BACKUPS


def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def backup_dir() -> Path:
    return detect.atropos_home() / "backups"


def create(include_state_db=True) -> dict:
    """Create a full backup tarball. Returns summary dict."""
    ts = _ts()
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"atropos_backup_{ts}.tar.gz"

    items = {}

    # config
    cfg_path = config.config_path()
    if cfg_path.exists():
        items["config.yaml"] = cfg_path

    # hacks
    hacks_dir = Path(__file__).resolve().parent.parent / "hacks"
    if hacks_dir.exists():
        items["hacks"] = hacks_dir

    # patches (backup copies)
    patches_dir = Path(__file__).resolve().parent.parent / "patches"
    if patches_dir.exists():
        items["patches"] = patches_dir

    # state.db
    if include_state_db:
        db = detect.hermes_home() / "state.db"
        if db.exists():
            items["state.db"] = db

    # sessions (as jsonl export, not raw sqlite)
    sessions_dir = detect.hermes_home() / "sessions"
    if sessions_dir.exists():
        items["sessions"] = sessions_dir

    # hooks
    hooks_dir = detect.hermes_home() / "hooks"
    if hooks_dir.exists():
        items["hooks"] = hooks_dir

    # scripts (only .py/.sh, no pycache)
    scripts_dir = detect.hermes_home() / "scripts"
    if scripts_dir.exists():
        items["scripts"] = scripts_dir

    with tarfile.open(dest, "w:gz") as tar:
        for arcname, src in items.items():
            if src.is_dir():
                tar.add(src, arcname=arcname, recursive=True, filter=lambda t: (
                    None if "__pycache__" in t.name or t.name.endswith(".pyc") else t
                ))
            else:
                tar.add(src, arcname=arcname)

    # prune to retention
    pruned = prune(_retention())

    return {
        "ok": True,
        "path": str(dest),
        "size_mb": round(dest.stat().st_size / (1024 * 1024), 2),
        "items": list(items.keys()),
        "pruned": pruned,
        "ts": ts,
    }


def list_backups() -> list:
    bdir = backup_dir()
    if not bdir.exists():
        return []
    backups = []
    for f in sorted(bdir.glob("atropos_backup_*.tar.gz"), reverse=True):
        backups.append({
            "name": f.name,
            "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
            "date": f.stat().st_mtime,
        })
    return backups


def prune(keep: int = MAX_BACKUPS) -> list:
    """Delete old backups beyond `keep`. Returns removed names."""
    backups = list_backups()
    removed = []
    for b in backups[keep:]:
        try:
            (backup_dir() / b["name"]).unlink()
            removed.append(b["name"])
        except Exception:
            pass
    return removed


def restore(name: str) -> dict:
    """Restore from a backup tarball. Returns summary."""
    bdir = backup_dir()
    src = bdir / name
    if not src.exists():
        return {"ok": False, "error": f"backup not found: {name}"}
    restores = []
    try:
        with tarfile.open(src, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "config.yaml":
                    dest = config.config_path()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    extract = tar.extractfile(member)
                    if extract:
                        dest.write_bytes(extract.read())
                        restores.append("config.yaml")
                elif member.name == "state.db":
                    dest = detect.hermes_home() / "state.db"
                    extract = tar.extractfile(member)
                    if extract:
                        # restore into a staged copy to avoid corrupting live db
                        tmp = detect.hermes_home() / "state.db.restore"
                        tmp.write_bytes(extract.read())
                        shutil.move(str(tmp), str(dest))
                        restores.append("state.db")
                elif member.name == "hacks":
                    dest = Path(__file__).resolve().parent.parent / "hacks"
                    tar.extract(member, path=str(dest.parent), filter="data")
                    restores.append("hacks")
                elif member.name == "hooks":
                    dest = detect.hermes_home() / "hooks"
                    tar.extract(member, path=str(dest.parent), filter="data")
                    restores.append("hooks")
                elif member.name == "patches":
                    dest = Path(__file__).resolve().parent.parent / "patches"
                    tar.extract(member, path=str(dest.parent), filter="data")
                    restores.append("patches")
                elif member.name == "sessions":
                    dest = detect.hermes_home() / "sessions"
                    tar.extract(member, path=str(dest.parent), filter="data")
                    restores.append("sessions")
                elif member.name == "scripts":
                    dest = detect.hermes_home() / "scripts"
                    tar.extract(member, path=str(dest.parent), filter="data")
                    restores.append("scripts")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "restored": restores, "from": name}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore", type=str, default=None)
    ap.add_argument("--prune", type=int, default=None)
    args = ap.parse_args()
    if args.list:
        for b in list_backups():
            print(f"  {b['name']}  ({b['size_mb']} MB)")
    elif args.restore:
        result = restore(args.restore)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.prune:
        removed = prune(args.prune)
        print(f"  pruned: {removed}")
    else:
        result = create()
        print(json.dumps(result, indent=2, ensure_ascii=False))