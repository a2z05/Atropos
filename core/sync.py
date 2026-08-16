#!/usr/bin/env python3
"""Atropos multi-backend sync engine — delta sync, stdlib only.

State lives under ``{atropos_home}/sync/``::

    index.json                        {rel_path: {hash, size_mb, mtime, version}}
    objects/<key>/<version>.json      per-file version history (capped at 10)
    objects/<key>/history.json        capped list of accepted writes (for diff)
    conflicts/<ts>/                   loser copies + <rel>.meta.json
    peers.json                        [{id, name, backend, last_seen, pending}]
    pair_codes.json                   active 6-digit codes with expiry

Managed content EXCLUDES secrets, .env, state.db, __pycache__, .git,
node_modules, the sync/ store itself, and anything named ``*_TOKEN*``.

Backends (registry ``get_backend(name)``)::

    file    local filesystem path backend (default {atropos_home}/sync/mirror/)
    server  REST sync server via urllib (Bearer token, 15s timeout)
    pair    direct two-device exchange via a staging dir or relay
    github  GITHUB_TOKEN REST (fails clearly when no token)

Delta model: a file is ``changed`` when its current hash differs from the
last-synced hash recorded in the local index. ``diff`` compares the local
scan against a remote index; a key whose hash diverged on *both* sides since
the last common sync is a conflict, resolved last-writer-wins by mtime with
the loser preserved under sync/conflicts/<ts>/.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import detect, settings

# ── constants ──────────────────────────────────────────────────────────────
SENSITIVE_NAMES = {
    "secrets.json", ".env", "state.db", "auth_token",
    ".gh_backup_token", "auth.json",
}
# Patterns applied to relative paths (case-insensitive).
SENSITIVE_PATTERNS = [
    re.compile(r"(^|/)secrets(/|$)", re.IGNORECASE),
    re.compile(r"__pycache__", re.IGNORECASE),
    re.compile(r"\.pyc$", re.IGNORECASE),
    re.compile(r"\.pyo$", re.IGNORECASE),
    re.compile(r"(^|/)node_modules(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.git(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)sync(/|$)", re.IGNORECASE),
    re.compile(r"_TOKEN", re.IGNORECASE),
    re.compile(r"(^|/)\.(history|written\.json|modes\.json)", re.IGNORECASE),
]

# Directories under the atropos home whose whole contents are managed.
MANAGED_DIRS = [
    "identity", "configs", "mcp", "models", "webhooks",
    "routing", "links", "commands", "memory", "skills",
]
# Root-level managed files (the canonical stores the rest of the system uses).
MANAGED_ROOT_FILES = [
    "config.yaml", "guest_persona.md",
    "mcp_servers.json", "models.json", "webhooks.json",
    "commands.json", "links.json",
]
# Top-level dir names never scanned.
_IGNORED_DIRS = {"sync", "__pycache__", ".git", "node_modules"}
HISTORY_CAP = 10


# ── paths ──────────────────────────────────────────────────────────────────
def sync_dir() -> Path:
    """Base directory for sync state (~/.atropos/sync)."""
    d = detect.atropos_home() / "sync"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return sync_dir() / "index.json"


def _objects_dir() -> Path:
    d = sync_dir() / "objects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _conflicts_dir() -> Path:
    d = sync_dir() / "conflicts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _peers_path() -> Path:
    return sync_dir() / "peers.json"


def _pair_codes_path() -> Path:
    return sync_dir() / "pair_codes.json"


# ── internal JSON helpers ──────────────────────────────────────────────────
def _load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_index() -> dict:
    data = _load_json(_index_path(), {})
    return data if isinstance(data, dict) else {}


def _save_index(idx: dict):
    _save_json(_index_path(), idx)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_dir() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ── sensitive file detection ───────────────────────────────────────────────
def _is_sensitive(rel: str) -> bool:
    """True when a relative path must NEVER be synced."""
    rel = rel.replace("\\", "/")
    name = rel.split("/")[-1]
    if name in SENSITIVE_NAMES:
        return True
    for pat in SENSITIVE_PATTERNS:
        if pat.search(rel):
            return True
    return False


def secrets_excluded() -> list:
    """Return the sensitive names/patterns the engine refuses to sync.

    Used by the CLI/dashboard to explain why a file was skipped.
    """
    out = sorted(SENSITIVE_NAMES)
    out.extend(p.pattern for p in SENSITIVE_PATTERNS)
    return out


# ── managed file scan ──────────────────────────────────────────────────────
def managed_files(base_dir: str | Path = None) -> list:
    """Sorted relative paths of managed content under ``base_dir``.

    base_dir defaults to detect.atropos_home(). Skips pycache/.git/
    node_modules/sync and every sensitive name. This is the hard allow-list —
    nothing outside it is ever synced.
    """
    if base_dir is None:
        base_dir = detect.atropos_home()
    base = Path(base_dir)
    if not base.exists():
        return []

    rels = []
    for fname in MANAGED_ROOT_FILES:
        p = base / fname
        if p.is_file():
            rels.append(fname)

    for dname in MANAGED_DIRS:
        d = base / dname
        if not d.is_dir() or d.name in _IGNORED_DIRS:
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(base)).replace("\\", "/")
            if _is_sensitive(rel):
                continue
            rels.append(rel)

    # drop duplicates (a root file can't also be under a managed dir, but be safe)
    return sorted(set(rels))


def scan(base_dir: str | Path = None) -> dict:
    """Scan managed files → {rel_path: {hash, mtime, size}}.

    hash is the sha256 hex digest of the file bytes; mtime is st_mtime.
    """
    if base_dir is None:
        base_dir = detect.atropos_home()
    base = Path(base_dir)
    result = {}
    for rel in managed_files(base):
        fp = base / rel
        try:
            data = fp.read_bytes()
            st = fp.stat()
        except OSError:
            continue
        result[rel] = {
            "hash": hashlib.sha256(data).hexdigest(),
            "mtime": st.st_mtime,
            "size": len(data),
        }
    return result


# ── diff engine ────────────────────────────────────────────────────────────
def diff(local: dict, remote: dict) -> tuple:
    """Compare a local scan against a remote index.

    Returns (to_push, to_pull, conflicts) — each a sorted list of rel paths.

    Rules per key:
      * local only        → to_push
      * remote only       → to_pull
      * same hash         → unchanged (skipped)
      * different hash:
          - when the local entry carries ``_idx_hash`` (the last-synced hash):
              both sides diverged from it           → conflict
              only remote diverged                   → to_pull
              only local diverged / baseline absent  → to_push
          - without a baseline (plain scan() results), a differing hash is
            reported as to_push (nothing proves the remote changed).
    """
    to_push, to_pull, conflicts = [], [], []
    shared = set(local) & set(remote)
    to_push = [k for k in local if k not in remote]
    to_pull = [k for k in remote if k not in local]

    for k in shared:
        lh = local[k].get("hash")
        rh = remote[k].get("hash")
        if lh == rh:
            continue
        baseline = local[k].get("_idx_hash")
        if baseline is None:
            to_push.append(k)
            continue
        local_changed = lh != baseline
        remote_changed = rh != baseline
        if local_changed and remote_changed:
            conflicts.append(k)
        elif remote_changed and not local_changed:
            to_pull.append(k)
        else:
            to_push.append(k)

    return sorted(to_push), sorted(to_pull), sorted(conflicts)


# ── version history ────────────────────────────────────────────────────────
def _key(rel: str) -> str:
    """Filesystem-safe object key for a rel path."""
    return rel.replace("/", "__").replace("\\", "__")


def _append_version(rel: str, entry: dict) -> int:
    """Record an accepted write: objects/<key>/<version>.json + capped history."""
    ver = int(entry.get("version", 0))
    odir = _objects_dir() / _key(rel)
    odir.mkdir(parents=True, exist_ok=True)
    _save_json(odir / f"{ver}.json", entry)
    hist = _load_json(odir / "history.json", [])
    if not isinstance(hist, list):
        hist = []
    hist.append(entry)
    hist = hist[-HISTORY_CAP:]
    _save_json(odir / "history.json", hist)
    return ver


def _version_count(rel: str) -> int:
    """Number of accepted writes recorded for a rel path."""
    hist = _load_json(_objects_dir() / _key(rel) / "history.json", [])
    return len(hist) if isinstance(hist, list) else 0


# ── path safety ────────────────────────────────────────────────────────────
def _safe_rel(rel: str) -> bool:
    """True when a remote-provided rel can be written into the home safely."""
    rel = rel.replace("\\", "/")
    if _is_sensitive(rel):
        return False
    if rel.startswith("/"):
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return True


# ── conflict resolution ────────────────────────────────────────────────────
def _resolve_conflict(rel: str, local_hash: str, local_bytes: bytes,
                      local_mtime: float, remote_hash: str, remote_bytes: bytes,
                      remote_mtime: float) -> str:
    """Last-writer-wins by mtime. Returns 'local' or 'remote'.

    The loser is copied verbatim to sync/conflicts/<ts>/<rel> with a
    `<rel>.meta.json` recording the resolution.
    """
    if local_mtime >= remote_mtime:
        winner, loser = "local", remote_bytes
    else:
        winner, loser = "remote", local_bytes

    ts = _ts_dir()
    cdir = _conflicts_dir() / ts
    cdir.mkdir(parents=True, exist_ok=True)
    loser_path = cdir / rel
    loser_path.parent.mkdir(parents=True, exist_ok=True)
    loser_path.write_bytes(loser)
    (cdir / f"{rel.replace('/', '__')}.meta.json").write_text(json.dumps({
        "rel": rel,
        "loser_hash": remote_hash if winner == "local" else local_hash,
        "winner": winner,
        "ts": _now_iso(),
    }, indent=2), encoding="utf-8")
    return winner


# ── backends ───────────────────────────────────────────────────────────────
class FileBackend:
    """Local filesystem backend: plain files under <path>/<key> + index.json."""

    def __init__(self, target: str = None):
        self._root = Path(target) if target else sync_dir() / "mirror"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        key = key.replace("\\", "/")
        return self._root / key

    def push(self, key: str, data: bytes):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def pull(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise FileNotFoundError(key)
        return p.read_bytes()

    def list(self) -> list:
        keys = []
        if self._root.exists():
            for f in sorted(self._root.rglob("*")):
                if f.is_file() and f.name != "index.json":
                    keys.append(str(f.relative_to(self._root)).replace("\\", "/"))
        return keys

    def delete(self, key: str):
        p = self._path(key)
        if p.is_file():
            p.unlink()

    def load_index(self) -> dict:
        data = _load_json(self._root / "index.json", {})
        return data if isinstance(data, dict) else {}

    def save_index(self, idx: dict):
        (self._root / "index.json").write_text(
            json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")


class ServerBackend:
    """REST sync server backend via urllib.request.

    GET/PUT/DELETE ``{sync.server.url}/sync/<key>`` with an optional
    ``Authorization: Bearer <token>`` header. 15s timeout. 404 → FileNotFoundError.
    """

    TIMEOUT = 15

    def __init__(self, target: str = None):
        self._url = (target if target else settings.get("sync.server.url", "")).rstrip("/")
        self._token = settings.get("sync.server.token", "")
        if not self._url:
            raise ValueError("sync server not configured")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/octet-stream"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _url_for(self, key: str) -> str:
        return f"{self._url}/sync/{key}"

    def push(self, key: str, data: bytes):
        req = urllib.request.Request(self._url_for(key), data=data,
                                     method="PUT", headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
            resp.read()

    def pull(self, key: str) -> bytes:
        req = urllib.request.Request(self._url_for(key), method="GET",
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FileNotFoundError(key)
            raise

    def list(self) -> list:
        req = urllib.request.Request(f"{self._url}/sync/", method="GET",
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return sorted(data) if isinstance(data, list) else []
        except Exception:
            return []

    def delete(self, key: str):
        req = urllib.request.Request(self._url_for(key), method="DELETE",
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def load_index(self) -> dict:
        req = urllib.request.Request(f"{self._url}/sync/_index", method="GET",
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_index(self, idx: dict):
        url = f"{self._url}/sync/_index"
        payload = json.dumps(idx).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="PUT",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
            resp.read()


class PairBackend:
    """Direct pair exchange backend: staging dir under sync/pair_exchange/<code>/.

    Also supports relaying through ``POST/GET {url}/sync/pair/exchange``.
    """

    def __init__(self, target: str = None, relay_url: str = ""):
        self._relay = (relay_url or "").rstrip("/")
        self._stage = Path(target) if target else sync_dir() / "pair_exchange" / "default"
        self._stage.mkdir(parents=True, exist_ok=True)

    def push(self, key: str, data: bytes):
        dest = self._stage / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if self._relay:
            self._relay_send(key, data)

    def pull(self, key: str) -> bytes:
        if self._relay:
            try:
                return self._relay_fetch(key)
            except Exception:
                pass
        src = self._stage / key
        if not src.is_file():
            raise FileNotFoundError(key)
        return src.read_bytes()

    def list(self) -> list:
        keys = []
        if self._stage.exists():
            for f in sorted(self._stage.rglob("*")):
                if f.is_file() and f.name != "index.json":
                    keys.append(str(f.relative_to(self._stage)).replace("\\", "/"))
        return keys

    def delete(self, key: str):
        p = self._stage / key
        if p.is_file():
            p.unlink()

    def load_index(self) -> dict:
        data = _load_json(self._stage / "index.json", {})
        return data if isinstance(data, dict) else {}

    def save_index(self, idx: dict):
        (self._stage / "index.json").write_text(
            json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- relay helpers -- #
    def _relay_send(self, key: str, data: bytes):
        url = f"{self._relay}/sync/pair/exchange"
        payload = json.dumps(
            {"key": key,
             "data": base64.b64encode(data).decode("ascii")}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

    def _relay_fetch(self, key: str) -> bytes:
        url = f"{self._relay}/sync/pair/exchange?key={urllib.parse.quote(key)}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if "data" not in payload:
                raise FileNotFoundError(key)
            return base64.b64decode(payload["data"])


class GitHubBackend:
    """GitHub REST backend driven by GITHUB_TOKEN.

    Without a token this fails with a clear unsupported error — it never
    fabricates authentication or silently degrades.
    """

    def __init__(self, target: str = None):
        self._token = os.environ.get("GITHUB_TOKEN", "")
        if not self._token:
            raise ValueError(
                "github backend requires GITHUB_TOKEN env var to be set; "
                "no token found (authentication is never fabricated)"
            )
        self._repo = (target or "").strip()
        if not self._repo:
            raise ValueError("github backend requires a repo target (owner/repo)")

    def push(self, key: str, data: bytes):
        raise NotImplementedError("github backend: push not yet implemented")

    def pull(self, key: str) -> bytes:
        raise NotImplementedError("github backend: pull not yet implemented")

    def list(self) -> list:
        raise NotImplementedError("github backend: list not yet implemented")

    def delete(self, key: str):
        raise NotImplementedError("github backend: delete not yet implemented")


_BACKENDS = {
    "file": FileBackend,
    "server": ServerBackend,
    "pair": PairBackend,
    "github": GitHubBackend,
}


def get_backend(name: str, target: str = None):
    """Instantiate a backend by name; ``target`` overrides its default config."""
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(
            f"unknown backend {name!r} — available: {', '.join(sorted(_BACKENDS))}"
        )
    kwargs = {"target": target} if target is not None else {}
    return cls(**kwargs)


# ── remote index reconstruction ────────────────────────────────────────────
def _reconstruct_remote_index(backend) -> dict:
    """Fall back to hashing every object the backend lists."""
    idx = {}
    for key in backend.list():
        if not _safe_rel(key):
            continue
        try:
            data = backend.pull(key)
        except Exception:
            continue
        idx[key] = {
            "hash": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "mtime": 0,
            "version": 0,
        }
    return idx


def _pull_remote_index(backend) -> dict:
    """Best-effort remote index: backend metadata first, else reconstruction."""
    try:
        idx = backend.load_index()
    except Exception:
        idx = {}
    if not idx:
        idx = _reconstruct_remote_index(backend)
    return idx if isinstance(idx, dict) else {}


# ── peer tracking ──────────────────────────────────────────────────────────
def _load_peers() -> list:
    data = _load_json(_peers_path(), [])
    return data if isinstance(data, list) else []


def _save_peers(peers: list):
    _save_json(_peers_path(), peers)


def _touch_peer(backend_name: str, name: str = None) -> str:
    """Record/refresh a peer entry on a successful sync. Returns the peer id."""
    label = name or backend_name
    pid = f"{backend_name}:{label}"
    peers = _load_peers()
    for p in peers:
        if p.get("id") == pid:
            p["last_seen"] = _now_iso()
            p["pending"] = []
            _save_peers(peers)
            return pid
    peers.append({
        "id": pid, "name": label, "backend": backend_name,
        "last_seen": _now_iso(), "pending": [],
    })
    _save_peers(peers)
    return pid


def sync_status() -> dict:
    """Per-peer status: {peer_id: {name, backend, last_seen, pending}}.

    ``pending`` is live: the managed files whose current hash differs from
    the last-synced index (i.e. unsynced local edits).
    """
    idx = _load_index()
    cur = scan()
    pending = sorted(
        rel for rel, info in cur.items()
        if idx.get(rel, {}).get("hash") != info["hash"]
    )
    result = {}
    for p in _load_peers():
        result[p.get("id", "")] = {
            "name": p.get("name", ""),
            "backend": p.get("backend", ""),
            "last_seen": p.get("last_seen"),
            "pending": pending,
        }
    return result


# ── delta push ─────────────────────────────────────────────────────────────
def sync_push(backend_name: str, target: str = None, backend=None) -> dict:
    """Upload only the changed files to a backend.

    Returns {ok, pushed, errors}. ``backend`` may be injected for tests (a
    recording wrapper, a stub, etc.); otherwise the registry is used.
    """
    if backend is None:
        backend = get_backend(backend_name, target=target)
    local = scan()
    idx = _load_index()

    pushed = []
    errors = []
    for rel in sorted(local):
        info = local[rel]
        if idx.get(rel, {}).get("hash") == info["hash"]:
            continue  # unchanged since last sync
        try:
            data = (detect.atropos_home() / rel).read_bytes()
            backend.push(rel, data)
            ver = int(idx.get(rel, {}).get("version", 0)) + 1
            entry = {
                "hash": info["hash"],
                "size_mb": round(info["size"] / (1024 * 1024), 6),
                "size": info["size"],
                "mtime": info["mtime"],
                "version": ver,
            }
            idx[rel] = entry
            _append_version(rel, {**entry, "ts": _now_iso(), "action": "push"})
            pushed.append(rel)
        except Exception as e:
            errors.append({"path": rel, "error": str(e)})

    if idx:
        _save_index(idx)
        try:
            backend.save_index(idx)  # keep the backend's metadata in step
        except Exception:
            pass
    if pushed:
        _touch_peer(backend_name, backend_name)
    return {"ok": not errors, "pushed": pushed, "errors": errors}


# ── delta pull ─────────────────────────────────────────────────────────────
def sync_pull(backend_name: str, source: str = None, backend=None) -> dict:
    """Fetch only the changed files from a backend and write them home.

    Returns {ok, pulled, conflicts_resolved, errors}. Writes the local index
    and per-object version history for every accepted write.
    """
    if backend is None:
        backend = get_backend(backend_name, target=source)
    local = scan()
    idx = _load_index()
    remote = _pull_remote_index(backend)

    # local view carries the last-synced hash so diff can spot real conflicts
    local_view = {}
    for rel, info in local.items():
        prev = idx.get(rel, {})
        local_view[rel] = {**info, "_idx_hash": prev.get("hash")}

    _to_push, to_pull, conflicts = diff(local_view, remote)

    pulled = []
    resolved = []
    errors = []
    home = detect.atropos_home()

    for rel in conflicts:
        if rel not in remote or not _safe_rel(rel):
            continue
        try:
            remote_data = backend.pull(rel)
            remote_hash = hashlib.sha256(remote_data).hexdigest()
            local_fp = home / rel
            local_data = local_fp.read_bytes() if local_fp.is_file() else b""
            local_mtime = local_fp.stat().st_mtime if local_fp.is_file() else 0
            remote_mtime = float(remote.get(rel, {}).get("mtime", 0) or 0)

            winner = _resolve_conflict(
                rel,
                local_hash=local.get(rel, {}).get("hash", ""), local_bytes=local_data,
                local_mtime=local_mtime,
                remote_hash=remote_hash, remote_bytes=remote_data,
                remote_mtime=remote_mtime if remote_mtime else local_mtime - 1,
            )
            if winner == "remote":
                local_fp.parent.mkdir(parents=True, exist_ok=True)
                local_fp.write_bytes(remote_data)

            final_hash = remote_hash if winner == "remote" else local.get(rel, {}).get("hash", "")
            ver = int(idx.get(rel, {}).get("version", 0)) + 1
            idx[rel] = {
                "hash": final_hash,
                "size_mb": round(len(remote_data) / (1024 * 1024), 6),
                "size": len(remote_data),
                "mtime": time.time(),
                "version": ver,
            }
            if remote.get(rel, {}).get("version"):
                idx[rel]["remote_version"] = remote[rel]["version"]
            _append_version(rel, {
                **idx[rel], "ts": _now_iso(), "action": "conflict_resolved",
            })
            resolved.append(rel)
        except Exception as e:
            errors.append({"path": rel, "error": str(e)})

    for rel in to_pull:
        if not _safe_rel(rel):
            continue
        try:
            data = backend.pull(rel)
        except FileNotFoundError:
            continue
        except Exception as e:
            errors.append({"path": rel, "error": str(e)})
            continue
        fp = home / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(data)
        h = hashlib.sha256(data).hexdigest()
        ver = int(idx.get(rel, {}).get("version", 0)) + 1
        idx[rel] = {
            "hash": h,
            "size_mb": round(len(data) / (1024 * 1024), 6),
            "size": len(data),
            "mtime": time.time(),
            "version": ver,
        }
        _append_version(rel, {**idx[rel], "ts": _now_iso(), "action": "pull"})
        pulled.append(rel)

    if idx:
        _save_index(idx)
    if pulled or resolved:
        _touch_peer(backend_name, backend_name)
    return {
        "ok": not errors,
        "pulled": pulled,
        "conflicts_resolved": resolved,
        "errors": errors,
    }


# ── pair flow ──────────────────────────────────────────────────────────────
def host_pair() -> dict:
    """Generate a 6-digit pair code with expiry = now + sync.pair_ttl_hours.

    Returns {code, expires} and records it in pair_codes.json.
    """
    code = f"{secrets.randbelow(900000) + 100000}"
    ttl = int(settings.get("sync.pair_ttl_hours", 1))
    expires = time.time() + ttl * 3600
    codes = _load_json(_pair_codes_path(), [])
    if not isinstance(codes, list):
        codes = []
    codes.append({"code": code, "expires": expires, "ts": _now_iso()})
    _save_json(_pair_codes_path(), codes)
    return {"code": code, "expires": expires}


def join_pair(code: str) -> dict:
    """Join a pair session. Returns {ok, target, relay_url}.

    Raises ValueError when the code is unknown, and ValueError("pair code
    expired") when it has expired.
    """
    code = (code or "").strip()
    codes = _load_json(_pair_codes_path(), [])
    if not isinstance(codes, list):
        codes = []
    for entry in codes:
        if entry.get("code") == code:
            if float(entry.get("expires", 0)) < time.time():
                raise ValueError("pair code expired")
            stage = sync_dir() / "pair_exchange" / code
            stage.mkdir(parents=True, exist_ok=True)
            return {
                "ok": True,
                "target": str(stage),
                "relay_url": settings.get("sync.server.url", "").rstrip("/"),
            }
    raise ValueError(f"pair code not found: {code!r}")


def prune_pair_codes() -> list:
    """Drop expired pair codes. Returns the removed codes."""
    codes = _load_json(_pair_codes_path(), [])
    if not isinstance(codes, list):
        return []
    now = time.time()
    kept, removed = [], []
    for entry in codes:
        if float(entry.get("expires", 0)) >= now:
            kept.append(entry)
        else:
            removed.append(entry.get("code", ""))
    _save_json(_pair_codes_path(), kept)
    return removed


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(sync_status(), indent=2, ensure_ascii=False))
    else:
        print(sync_dir())
