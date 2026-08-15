#!/usr/bin/env python3
"""Atropos files — read-only files panel, stdlib only.

Three capabilities, all strictly read-only:

  * ``list_dir``  — bounded directory listing (max 200 entries) with
                    subdirectory support,
  * ``read_file`` — text file preview (max 200 KB); binary files are
                    rejected via a NUL-byte check,
  * ``search``    — filename substring walk (max 5000 files visited,
                    max 100 hits), case-insensitive.

Every path is resolved against the repo root and any attempt to escape it
(absolute paths, ``..`` segments) is rejected with {ok: False, error}.
No writes are ever performed.
"""
import os
import re
from pathlib import Path

MAX_ENTRIES = 200
MAX_READ_BYTES = 200_000
MAX_SEARCH_FILES = 5_000
MAX_HITS = 100


def repo_root() -> Path:
    """Repo root: the directory containing the core package."""
    return Path(__file__).resolve().parent.parent


def _safe_resolve(path_str: str, root: Path) -> tuple:
    """Resolve path_str against root; reject escapes.

    Returns (resolved_path, None) on success or (None, error_message).
    Absolute paths are rejected outright, as are any paths that resolve
    outside the root (e.g. ``..`` segments).
    """
    path_str = (path_str or "").strip()
    if not path_str:
        return root, None
    if path_str.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path_str):
        return None, "absolute paths are not allowed"
    candidate = (root / path_str).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None, f"path escapes the allowed root: {path_str}"
    return candidate, None


def list_dir(path: str | None = None, root: Path | None = None) -> dict:
    """List a directory inside root (default: repo root).

    Returns {ok: True, path, root, entries: [{name, type, size, mtime}]}
    with at most 200 entries (directories first, alphabetical). Missing
    or non-directory targets and escape attempts return {ok: False, error}.
    """
    root = Path(root) if root else repo_root()
    target, err = _safe_resolve(path or "", root)
    if err:
        return {"ok": False, "error": err}
    if not target.exists():
        return {"ok": False, "error": f"no such file or directory: {path or ''}"}
    if not target.is_dir():
        return {"ok": False, "error": f"not a directory: {path or ''}"}

    entries = []
    try:
        names = sorted(os.listdir(target))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    for name in names:
        if len(entries) >= MAX_ENTRIES:
            break
        full = target / name
        try:
            st = full.stat()
            entries.append({
                "name": name,
                "type": "dir" if full.is_dir() else "file",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except OSError:
            continue
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"ok": True, "path": str(target), "root": str(target), "entries": entries}


def read_file(path: str, max_bytes: int = MAX_READ_BYTES, root: Path | None = None) -> dict:
    """Read a text file for preview. Binary files are rejected.

    Returns {ok: True, name, path, size, text} (truncated to max_bytes),
    or {ok: False, error} for escapes, missing files, directories and
    binary content (NUL byte detection). Never writes.
    """
    root = Path(root) if root else repo_root()
    target, err = _safe_resolve(path or "", root)
    if err:
        return {"ok": False, "error": err}
    if not target.exists():
        return {"ok": False, "error": f"no such file: {path or ''}"}
    if target.is_dir():
        return {"ok": False, "error": f"is a directory: {path or ''}"}

    max_bytes = max(1, int(max_bytes))
    try:
        with open(target, "rb") as f:
            data = f.read(max_bytes + 1)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if b"\x00" in data:
        return {"ok": False, "error": "binary file — preview not supported"}
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    return {
        "ok": True,
        "name": target.name,
        "path": str(target),
        "size": target.stat().st_size,
        "text": text,
        "truncated": truncated,
    }


def search(q: str = "", root: Path | None = None) -> dict:
    """Search for files whose name contains q (case-insensitive).

    Walks at most 5000 files and returns at most 100 hits as paths
    relative to root. Missing roots or bad escapes return {ok: False,
    error}. Returns {ok: True, q, hits, count}.
    """
    root = Path(root) if root else repo_root()
    base, err = _safe_resolve("", root)
    if err:
        return {"ok": False, "error": err}
    if not base.exists() or not base.is_dir():
        return {"ok": False, "error": f"not a directory: {root}"}
    q = (q or "").strip()
    if not q:
        return {"ok": True, "q": "", "hits": [], "count": 0}
    needle = q.lower()

    hits = []
    files_seen = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            files_seen += 1
            if files_seen > MAX_SEARCH_FILES:
                break
            if needle in name.lower():
                hits.append(str(Path(dirpath).relative_to(base) / name))
                if len(hits) >= MAX_HITS:
                    return {"ok": True, "q": q, "hits": hits, "count": len(hits)}
        if files_seen > MAX_SEARCH_FILES:
            break
    return {"ok": True, "q": q, "hits": hits, "count": len(hits)}


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "ls":
        print(json.dumps(list_dir(sys.argv[2] if len(sys.argv) > 2 else None),
                         indent=2, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "read":
        print(json.dumps(read_file(sys.argv[2]), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(search(sys.argv[1] if len(sys.argv) > 1 else ""),
                         indent=2, ensure_ascii=False))
