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
import hashlib
import io
import json
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, settings

# ── v18 H.2 manifest helpers ───────────────────────────────────────────────
_SECRET_NAMES = ("auth_token", "telegram.token", "token", "secret", "key",
                 "password", "credential", ".env")


class _MemFile:
    """In-memory tar member (used for the manifest)."""

    def __init__(self, data: bytes):
        self.data = data


def _memfile(obj: dict) -> _MemFile:
    return _MemFile(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def _version() -> str:
    try:
        repo = Path(__file__).resolve().parent.parent
        return (repo / "VERSION").read_text().strip()
    except Exception:
        return "unknown"


def _size_of(src) -> int:
    if isinstance(src, _MemFile):
        return len(src.data)
    if src.is_dir():
        return sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
    try:
        return src.stat().st_size
    except OSError:
        return 0


def _sha1(src) -> str:
    if isinstance(src, _MemFile):
        return hashlib.sha1(src.data).hexdigest()  # noqa: S324 — checksum only
    h = hashlib.sha1()  # noqa: S324 — checksum only, not security
    try:
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                if p.is_file():
                    h.update(p.read_bytes())
        else:
            h.update(src.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()


def _secret_name(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in _SECRET_NAMES)

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

    # v18 H.2 complete scope — everything Atropos owns (secrets masked)
    ahome = detect.atropos_home()
    for sub in ("memory", "skills", "agents", "custom_filters", "slash",
                "bot_config.yaml", "activity.jsonl", "audit.jsonl",
                "router_history.json", "fate.json", "chat.db"):
        p = ahome / sub
        if p.exists():
            items[f"atropos/{sub}"] = p
    # hermes memories + skills (read-only copy, masked)
    for sub in ("memories", "skills"):
        p = detect.hermes_home() / sub
        if p.exists():
            items[f"hermes/{sub}"] = p
    # cron jobs from hermes
    cron_dir = detect.hermes_home() / "cron"
    if cron_dir.exists():
        items["hermes/cron"] = cron_dir

    # manifest written BEFORE the tar so it rides inside the backup
    manifest = {
        "version": _version(),
        "ts": ts,
        "files": {k: _size_of(v) for k, v in items.items()},
        "checksums": {k: _sha1(v) for k, v in items.items()},
        "secrets_masked": True,
    }
    items["MANIFEST.json"] = _memfile(manifest)

    with tarfile.open(dest, "w:gz") as tar:
        for arcname, src in items.items():
            if isinstance(src, _MemFile):
                info = tarfile.TarInfo(arcname)
                info.size = len(src.data)
                tar.addfile(info, io.BytesIO(src.data))
                continue
            if src.is_dir():
                tar.add(src, arcname=arcname, recursive=True, filter=lambda t: (
                    None if "__pycache__" in t.name or t.name.endswith(".pyc")
                    or _secret_name(t.name) else t
                ))
            else:
                tar.add(src, arcname=arcname, filter=lambda t: (
                    None if _secret_name(t.name) else t
                ))

    # prune to retention
    pruned = prune(_retention())

    # v18 E: one more thread woven — the weave counter
    try:
        from . import fate
        fate.weave(1)
    except Exception:
        pass

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
    for f in bdir.glob("atropos_backup_*.tar.gz"):
        backups.append({
            "name": f.name,
            "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
            "date": f.stat().st_mtime,
        })
    backups.sort(key=lambda b: b["date"], reverse=True)
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
    """Restore from a local backup tarball. Returns summary."""
    if isinstance(name, Path) and name.is_absolute():
        src = name
    else:
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
                elif member.name.startswith("atropos/"):
                    # v18 H.2: restore Atropos-side content into ~/.atropos
                    rel = member.name[len("atropos/"):]
                    dest = detect.atropos_home() / rel
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    extract = tar.extractfile(member)
                    if extract:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(extract.read())
                        restores.append(f"atropos/{rel}")
                elif member.name == "MANIFEST.json":
                    continue  # metadata, not content
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "restored": restores, "from": str(src)}


# ── multi-backend (v1.4 final polish) ──────────────────────────────────────
_EXCLUDED_DIRS = {"logs", "__pycache__", "node_modules", ".git", "sync",
                  "backups", "sessions", "hooks", "scripts", "cache"}


def content_items(home) -> dict:
    """Broad-scope backup content: {arcname -> Path}.

    Includes config.yaml, guest_persona.md, identity/, mcp/, models/,
    webhooks/, routing/, links/, commands/, memory/, patches/, templates/,
    dashboard/, hacks/ and the repo VERSION. Excludes logs/__pycache__/
    node_modules/.git/sync/objects, secrets files and state.db.
    """
    home = Path(home)
    items = {}

    def _add_dir(rel: Path, arc: str):
        if not rel.is_dir():
            return
        for f in sorted(rel.rglob("*")):
            if f.is_dir() and f.name in _EXCLUDED_DIRS:
                continue
            if f.name in _EXCLUDED_DIRS:
                continue
            if f.is_dir():
                continue
            r = f.relative_to(rel)
            if "__pycache__" in r.parts or f.suffix == ".pyc":
                continue
            if f.name in ("secrets.json", "auth_token") or "secret" in f.name.lower():
                continue
            if f.name == ".env" or f.name.endswith(".env") or f.name.startswith(".env"):
                continue
            items[f"{arc}/{r.as_posix()}"] = f

    _add_dir(home, "atropos")
    for d in ("config.yaml", "guest_persona.md"):
        p = home / d
        if p.is_file():
            items[d] = p
    for sub in ("identity", "mcp", "models", "webhooks", "routing", "links",
                "commands", "memory"):
        _add_dir(home / sub, f"atropos/{sub}")
    for sub in ("patches", "templates", "hacks"):
        p = Path(__file__).resolve().parent.parent / sub
        if p.is_dir():
            _add_dir(p, sub)
    repo = Path(__file__).resolve().parent.parent
    v = repo / "VERSION"
    if v.exists():
        items["VERSION"] = v
    return items


def _remote_dir() -> Path:
    return backup_dir() / "remote"


def manifest() -> dict:
    """{backend: [{name, size_mb, date}]} for pushed remote backups."""
    p = backup_dir() / "manifest.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_manifest(m: dict):
    p = backup_dir() / "manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_tarball(dest: Path) -> Path:
    """Pack content_items into dest.tar.gz. Returns dest path."""
    items = content_items(detect.atropos_home())
    with tarfile.open(dest, "w:gz") as tar:
        for arcname, src in items.items():
            if src.is_dir():
                tar.add(src, arcname=arcname, recursive=True, filter=lambda t: (
                    None if "__pycache__" in t.name or t.name.endswith(".pyc")
                    else t))
            else:
                tar.add(src, arcname=arcname)
    return dest


def create_backend(backend: str = "file") -> dict:
    """Build a tarball and push it to a backend (file|s3|server|github|pair).

    Records the push in manifest.json and runs prune_all on success.
    """
    provider = backend or "file"
    from . import settings as _st
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    ts = _ts()
    name = f"atropos_backup_{ts}.tar.gz"
    dest = bdir / name
    _build_tarball(dest)
    summary = {"ok": True, "path": str(dest), "size_mb": round(dest.stat().st_size / 1048576, 2), "ts": ts}
    try:
        if provider == "file":
            rd = _remote_dir() / "file"
            rd.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(dest), rd / name)
            summary["remote"] = str(rd / name)
        elif provider == "s3":
            from . import s3 as s3_mod
            ep = _st.get("backup.s3.endpoint", "")
            bucket = _st.get("backup.s3.bucket", "")
            ak = _st.get("backup.s3.access_key", "")
            sk = _st.get("backup.s3.secret_key", "")
            region = _st.get("backup.s3.region", "us-east-1")
            if not (ep and bucket and ak and sk):
                return {"ok": False, "error": "S3 not configured (backup.s3.endpoint/bucket/access_key/secret_key)"}
            client = s3_mod.S3Client(ep, bucket, region, ak, sk)
            client.put(f"atropos-backups/{name}", dest.read_bytes())
            summary["remote"] = f"s3://{bucket}/atropos-backups/{name}"
        elif provider == "server":
            import urllib.request
            url = _st.get("backup.server.url", "").rstrip("/")
            token = _st.get("backup.server.token", "")
            if not url:
                return {"ok": False, "error": "backup server not configured (backup.server.url)"}
            req = urllib.request.Request(f"{url}/backup/{name}", data=dest.read_bytes(),
                                         method="PUT")
            req.add_header("Content-Type", "application/octet-stream")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=60) as resp:
                summary["remote"] = f"server:{url}/{name}"
        elif provider == "github":
            import urllib.request, json as _json
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                return {"ok": False, "error": "GITHUB_TOKEN not set"}
            repo = os.environ.get("ATROPOS_GH_REPO", "arophin/Atropos")
            release_url = f"https://api.github.com/repos/{repo}/releases"
            req = urllib.request.Request(release_url, data=_json.dumps({
                "tag_name": f"backup-{ts}", "name": f"Atropos backup {ts}",
                "draft": True, "prerelease": True}).encode(),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                rel = _json.loads(resp.read().decode())
            upload_url = rel.get("upload_url", "").split("{")[0]
            if upload_url:
                up = urllib.request.Request(
                    f"{upload_url}?name={name}", data=dest.read_bytes(), method="POST",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(up, timeout=60) as resp:
                    summary["remote"] = f"github:{repo}/releases/{ts}"
        elif provider == "pair":
            pdir = _remote_dir() / "pair"
            pdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(dest), pdir / name)
            summary["remote"] = str(pdir / name)
        else:
            return {"ok": False, "error": f"unknown backend: {provider}"}
    except Exception as e:
        dest.unlink(missing_ok=True)
        return {"ok": False, "error": str(e)}

    m = manifest()
    m.setdefault(provider, []).append({"name": name, "size_mb": summary["size_mb"],
                                       "date": ts, "remote": summary.get("remote", "")})
    _save_manifest(m)
    pruned = prune_all()
    summary["pruned"] = pruned
    summary["backend"] = provider
    return summary


def list_backends() -> dict:
    """Per-backend {configured, connected, last_backup}."""
    from . import settings as _st
    out = {}
    for name in ("file", "s3", "server", "github", "pair"):
        rec = {"configured": False, "connected": None, "last_backup": None}
        m = manifest()
        if name in m and m[name]:
            rec["last_backup"] = m[name][-1]
        if name == "file":
            rec["configured"] = True
            rec["connected"] = True
        elif name == "s3":
            rec["configured"] = bool(_st.get("backup.s3.endpoint") and _st.get("backup.s3.bucket"))
        elif name == "server":
            rec["configured"] = bool(_st.get("backup.server.url"))
        elif name == "github":
            rec["configured"] = bool(os.environ.get("GITHUB_TOKEN"))
        elif name == "pair":
            rec["configured"] = True
        out[name] = rec
    return out


def restore_preview(name_or_path) -> list:
    """List the tarball members without extracting anything."""
    src = Path(name_or_path)
    if not src.is_absolute():
        src = backup_dir() / src
    if not src.exists():
        # look in the remote store too
        alt = _remote_dir() / "file" / src.name if src.name else None
        if alt and alt.exists():
            src = alt
        else:
            return []
    with tarfile.open(src, "r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def restore_backend(backend: str, name: str) -> dict:
    """Pull a backup tarball from a backend and restore it (preview + confirm
    handled by the caller)."""
    from . import settings as _st
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    tmp = bdir / f".restore_{name}"
    try:
        if backend == "file":
            src = _remote_dir() / "file" / name
            if not src.exists():
                src = backup_dir() / name
            shutil.copy2(str(src), tmp)
        elif backend == "s3":
            from . import s3 as s3_mod
            client = s3_mod.S3Client(_st.get("backup.s3.endpoint"), _st.get("backup.s3.bucket"),
                                     _st.get("backup.s3.region", "us-east-1"),
                                     _st.get("backup.s3.access_key"), _st.get("backup.s3.secret_key"))
            tmp.write_bytes(client.get(f"atropos-backups/{name}"))
        elif backend == "server":
            import urllib.request
            url = _st.get("backup.server.url", "").rstrip("/")
            token = _st.get("backup.server.token", "")
            req = urllib.request.Request(f"{url}/backup/{name}")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=60) as resp:
                tmp.write_bytes(resp.read())
        elif backend == "github":
            import urllib.request, json as _json
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                return {"ok": False, "error": "GITHUB_TOKEN not set"}
            repo = os.environ.get("ATROPOS_GH_REPO", "arophin/Atropos")
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases",
                                         headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                releases = _json.loads(resp.read().decode())
            asset_url = None
            for rel in releases:
                for asset in rel.get("assets", []):
                    if asset.get("name") == name:
                        asset_url = asset.get("url")
                        break
                if asset_url:
                    break
            if not asset_url:
                return {"ok": False, "error": f"github asset not found: {name}"}
            ar = urllib.request.Request(asset_url,
                                        headers={"Authorization": f"Bearer {token}",
                                                 "Accept": "application/octet-stream"})
            with urllib.request.urlopen(ar, timeout=120) as resp:
                tmp.write_bytes(resp.read())
        elif backend == "pair":
            src = _remote_dir() / "pair" / name
            if not src.exists():
                return {"ok": False, "error": f"pair backup not found: {name}"}
            shutil.copy2(str(src), tmp)
        else:
            return {"ok": False, "error": f"unknown backend: {backend}"}
        return restore(tmp)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": str(e)}
    finally:
        tmp.unlink(missing_ok=True)


def prune_all(retention: int = None, weekly: int = None) -> list:
    """Keep the newest N backups overall AND the newest per ISO week for the
    last `weekly` weeks. Returns removed names. Runs on create."""
    from . import settings as _st
    keep = retention if retention is not None else _retention()
    wk = weekly if weekly is not None else int(_st.get("backup.retention_weekly", 4) or 4)
    backups = list_backups()
    if not backups:
        return []
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    def _iso_week(ts: float) -> str:
        d = _dt.fromtimestamp(ts, tz=_tz.utc)
        return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"

    keep_set = set()
    # newest N overall
    keep_set.update(b["name"] for b in backups[:keep])
    # newest per week for the last `wk` weeks
    weeks = {}
    for b in backups:
        weeks.setdefault(_iso_week(b["date"]), []).append(b)
    # weeks are already sorted by date descending; take the first `wk` weeks
    ordered_weeks = sorted(weeks.keys(), reverse=True)[:wk]
    for w in ordered_weeks:
        keep_set.add(weeks[w][0]["name"])
    removed = []
    for b in backups:
        if b["name"] not in keep_set:
            try:
                (backup_dir() / b["name"]).unlink()
                removed.append(b["name"])
            except Exception:
                pass
    return removed


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