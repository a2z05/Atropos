"""File-write safety: guards, caps, diff tracking and diff collection.

Ported from hermes-agent/tools/path_security.py (containment checks),
agent/file_safety.py (write-deny list: credential paths, prefixes,
state.db / sessions / mcp-tokens / pairing under the active home),
tools/file_state.py (per-path staleness registry with read/write stamps),
tools/tool_output_limits.py (configurable max_bytes/max_lines/
max_line_length caps), tools/tool_result_storage.py (preview generation
and the persisted-output footer), tools/write_approval.py (stage_write /
list_pending / discard_pending / pending_count) and
tools/working_diff.py (collect_working_diff over subprocess git).

Deviations (deliberate, all stdlib-only):
* No config.yaml ``tool_output`` section in Atropos: caps are read from
  ``core.settings`` keys ``safety.output_max_bytes`` etc., defaulting to
  the Hermes constants; env vars ATROPOS_OUTPUT_MAX_BYTES/LINES/
  LINE_LENGTH override for tests.
* ``write_approval_enabled`` reads ``safety.write_approval`` from the
  same settings path instead of the per-subsystem ``<s>.<config_key>``
  keys; the gate is still off by default and the memory/skills
  subsystems are honoured for staging purposes.
* The gate always returns a decision with ``staged=True`` instead of
  ``stage=True`` (the Hermes ``stage`` outcome holds a path that Atropos
  callers never use), and never prompts inline — there is no interactive
  approval callback in Atropos, so memory foreground writes stage like
  every other surface.
* ``output_cap`` merges generate_preview's at-last-newline truncation
  with the tool_output_limits line/line-length caps and appends the
  persisted-output-style footer note from tool_result_storage.py.

Every entry point degrades to {'ok': ..., 'error': ...} instead of
raising for missing git / unreadable files / bad config.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    # guards + caps
    "guard_write", "output_cap", "generate_preview", "guard_read",
    "get_output_limits",
    # diff tracking
    "record_read", "record_diff", "note_write", "check_stale",
    "lock_path", "known_reads", "writes_since", "get_registry",
    "FileStateRegistry", "unified_diff", "get_diff_log",
    # approval staging (write_approval.py surface)
    "write_approval_enabled", "evaluate_gate", "stage_write",
    "list_pending", "get_pending", "discard_pending", "pending_count",
    # working-tree diff (working_diff.py surface)
    "collect_working_diff",
    # path security (path_security.py surface)
    "validate_within_dir", "has_traversal_component",
    "is_write_denied", "get_write_denied_error",
    # constants
    "DEFAULT_MAX_BYTES", "DEFAULT_MAX_LINES", "DEFAULT_MAX_LINE_LENGTH",
    "PERSISTED_OUTPUT_TAG", "PERSISTED_OUTPUT_CLOSING_TAG",
    "MEMORY", "SKILLS",
]

# ── output caps (tool_output_limits.py + tool_result_storage.py) ────────
DEFAULT_MAX_BYTES = 50_000       # terminal_tool.MAX_OUTPUT_CHARS
DEFAULT_MAX_LINES = 2000         # file_operations.MAX_LINES
DEFAULT_MAX_LINE_LENGTH = 2000   # file_operations.MAX_LINE_LENGTH

# write guard size cap (guard_write default)
DEFAULT_WRITE_MAX_BYTES = 1024 * 1024

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

# write_approval.py subsystem identifiers
MEMORY = "memory"
SKILLS = "skills"
_SUBSYSTEMS = (MEMORY, SKILLS)


def get_output_limits() -> Dict[str, int]:
    """Return resolved output limits (max_bytes/max_lines/max_line_length).

    Atropos has no ``tool_output`` config section (the Hermes
    tool_output_limits.py reader targets ``config.yaml``); the values come
    from ``core.settings`` keys ``safety.output_max_bytes`` etc. with the
    Hermes defaults as fallback. Env overrides
    ATROPOS_OUTPUT_MAX_BYTES/LINES/LINE_LENGTH win for tests. Never raises.
    """
    env = os.environ
    if "ATROPOS_OUTPUT_MAX_BYTES" in env:
        default_bytes = _coerce_positive_int(env["ATROPOS_OUTPUT_MAX_BYTES"], DEFAULT_MAX_BYTES)
    else:
        default_bytes = _settings_int("safety.output_max_bytes", DEFAULT_MAX_BYTES)
    if "ATROPOS_OUTPUT_MAX_LINES" in env:
        default_lines = _coerce_positive_int(env["ATROPOS_OUTPUT_MAX_LINES"], DEFAULT_MAX_LINES)
    else:
        default_lines = _settings_int("safety.output_max_lines", DEFAULT_MAX_LINES)
    if "ATROPOS_OUTPUT_MAX_LINE_LENGTH" in env:
        default_ll = _coerce_positive_int(env["ATROPOS_OUTPUT_MAX_LINE_LENGTH"], DEFAULT_MAX_LINE_LENGTH)
    else:
        default_ll = _settings_int("safety.output_max_line_length", DEFAULT_MAX_LINE_LENGTH)
    return {
        "max_bytes": default_bytes,
        "max_lines": default_lines,
        "max_line_length": default_ll,
    }


def _settings_int(key: str, default: int) -> int:
    try:
        from . import settings as _s
        return _coerce_positive_int(_s.get(key, default), default)
    except Exception:
        return default


def _coerce_positive_int(value: Any, default: int) -> int:
    """Return ``value`` as a positive int, or ``default`` on any issue."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    if iv <= 0:
        return default
    return iv


def generate_preview(content: str, max_chars: int = 2000) -> Tuple[str, bool]:
    """Truncate at last newline within max_chars. Returns (preview, has_more)."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def output_cap(text: str, max_chars: Optional[int] = None) -> dict:
    """Cap a tool output: per-line length, line count, then total chars.

    Returns {'ok': True, 'text': ..., 'truncated': bool, 'note': str}.
    The three caps mirror tool_output_limits.py; the truncation point
    follows tool_result_storage.py generate_preview (last newline within
    the budget), and the footer mirrors the <persisted-output> message
    when content was cut.
    """
    if not isinstance(text, str):
        text = str(text)
    limits = get_output_limits()
    if max_chars is None:
        max_chars = limits["max_bytes"]
    notes: list[str] = []

    max_ll = limits["max_line_length"]
    if max_ll > 0:
        cut = [ln if len(ln) <= max_ll else ln[:max_ll] + "… [truncated]"
               for ln in text.split("\n")]
        if any(len(a) != len(b) for a, b in zip(cut, text.split("\n"))):
            notes.append(f"per-line cap {max_ll}")
        text = "\n".join(cut)

    lines = text.split("\n")
    if len(lines) > limits["max_lines"]:
        keep = lines[:limits["max_lines"]]
        dropped = len(lines) - limits["max_lines"]
        text = "\n".join(keep) + f"\n… [truncated: {dropped} more lines]"
        notes.append(f"line cap {limits['max_lines']}")

    truncated = len(text) > max_chars
    if truncated:
        preview, _more = generate_preview(text, max_chars=max_chars)
        footer = (
            f"\n\n[Truncated: tool response was {len(text):,} chars. "
            f"Full output exceeds the {max_chars:,}-char cap.]"
        )
        text = preview + footer
        notes.append(f"char cap {max_chars}")

    return {"ok": True, "text": text, "truncated": truncated,
            "note": "; ".join(notes) or "no truncation"}


# ── write guards (path_security.py + agent/file_safety.py) ──────────────
def _expand_home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def _home_dirs() -> List[Path]:
    """Active app home(s): ATROPOS_HOME (or ~/.atropos) plus its parent."""
    dirs: List[Path] = []
    try:
        from . import detect
        base = detect.atropos_home().resolve()
    except Exception:
        base = Path(os.environ.get("ATROPOS_HOME", "") or
                    Path.home() / ".atropos").resolve()
    if base not in dirs:
        dirs.append(base)
    return dirs


def build_write_denied_paths(home: str) -> set:
    """Exact sensitive paths that must never be written (file_safety.py)."""
    h = Path(home).expanduser().resolve()
    home_dirs = _home_dirs()
    root_candidates = [Path(str(home_dirs[0].parent))] if home_dirs else []
    paths = [
        h / ".ssh" / "authorized_keys",
        h / ".ssh" / "id_rsa",
        h / ".ssh" / "id_ed25519",
        h / ".ssh" / "config",
        h / ".netrc",
        h / ".pgpass",
        h / ".npmrc",
        h / ".pypirc",
        h / ".git-credentials",
        *[d / ".env" for d in home_dirs],
        *[d / ".anthropic_oauth.json" for d in home_dirs],
        *[d / "cache" / "bws_cache.enc.json" for d in home_dirs],
        *[r / ".env" for r in root_candidates],
        *[r / ".anthropic_oauth.json" for r in root_candidates],
        *[r / "cache" / "bws_cache.enc.json" for r in root_candidates],
        Path("/etc/sudoers"), Path("/etc/passwd"), Path("/etc/shadow"),
    ]
    return {str(os.path.realpath(p)) for p in paths}


def build_write_denied_prefixes(home: str) -> List[str]:
    """Sensitive directory prefixes that must never be written."""
    h = Path(home).expanduser().resolve()
    prefixes = [
        h / ".ssh", h / ".aws", h / ".gnupg", h / ".kube",
        h / ".docker", h / ".azure",
        h / ".config" / "gh", h / ".config" / "gcloud",
        Path("/etc/sudoers.d"), Path("/etc/systemd"),
    ]
    return [str(p.resolve()) + os.sep for p in prefixes]


def _classify_write_denial(path: str) -> Optional[str]:
    """Return 'credential', 'safe_root', or None if writes are allowed."""
    home = _expand_home()
    resolved = str(Path(path).expanduser().resolve())

    if resolved in build_write_denied_paths(str(home)):
        return "credential"
    for prefix in build_write_denied_prefixes(str(home)):
        if resolved.startswith(prefix):
            return "credential"

    for base in _home_dirs():
        real = str(base)
        # Application-owned state — the agent's generic file tools must not
        # rewrite session history / credential stores.
        if resolved == os.path.join(real, "state.db"):
            return "credential"
        sessions_real = os.path.join(real, "sessions")
        if resolved == sessions_real or resolved.startswith(sessions_real + os.sep):
            return "credential"
        for sub in ("mcp-tokens", "pairing"):
            s_real = os.path.join(real, sub)
            if resolved == s_real or resolved.startswith(s_real + os.sep):
                return "credential"

    safe_roots = get_safe_write_roots()
    if safe_roots:
        allowed = any(
            resolved == root or resolved.startswith(root + os.sep)
            for root in safe_roots
        )
        if not allowed:
            return "safe_root"
    return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    return _classify_write_denial(path) is not None


def get_write_denied_error(path: str, *, verb: str = "Write") -> Optional[str]:
    """Return a model-facing error when writes to ``path`` are blocked."""
    denial = _classify_write_denial(path)
    if denial is None:
        return None
    if denial == "safe_root":
        roots_display = os.pathsep.join(sorted(get_safe_write_roots()))
        return (
            f"{verb} denied: '{path}' is outside ATROPOS_WRITE_SAFE_ROOT "
            f"({roots_display}). Unset the variable or add this path's directory prefix."
        )
    return f"{verb} denied: '{path}' is a protected system/credential file."


def get_safe_write_roots() -> set:
    """Resolved ATROPOS_WRITE_SAFE_ROOT paths (pathsep-separated)."""
    env = os.getenv("ATROPOS_WRITE_SAFE_ROOT", "")
    if not env:
        return set()
    roots: set = set()
    for path in env.split(os.pathsep):
        if path:
            try:
                roots.add(os.path.realpath(os.path.expanduser(path)))
            except (OSError, ValueError):
                continue
    return roots


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """Ensure *path* resolves to a location within *root* (path_security.py)."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """True if *path_str* contains ``..`` traversal components."""
    return ".." in Path(path_str).parts


def _app_state_dirs() -> List[Path]:
    """App-owned directories (sessions/, mcp-tokens/, pairing/) under home."""
    return [Path(os.path.join(str(d), name)) for d in _home_dirs()
            for name in ("sessions", "mcp-tokens", "pairing")]


def guard_read(path: str) -> dict:
    """Guard a file read: existence + read-deny for credential stores.

    Mirrors agent/file_safety.py get_read_block_error categories: internal
    credential stores under the app home (auth.json, .env, mcp-tokens/,
    pairing/) and project-local .env files. Returns
    {'ok': True, 'path': ...} or {'ok': False, 'error': ...}.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    resolved = str(p.resolve())
    home_dirs = [str(d) for d in _home_dirs()]

    for base in home_dirs:
        for blocked in (
            os.path.join(base, "auth.json"),
            os.path.join(base, "auth.lock"),
            os.path.join(base, ".anthropic_oauth.json"),
            os.path.join(base, ".env"),
        ):
            if resolved == blocked:
                return {"ok": False,
                        "error": f"read denied: '{path}' is a protected credential file."}
        for sub in ("mcp-tokens", "pairing"):
            s_real = os.path.join(base, sub)
            if resolved == s_real or resolved.startswith(s_real + os.sep):
                return {"ok": False,
                        "error": f"read denied: '{path}' is a protected credential file."}

    if p.name in {".env", ".env.local", ".env.development", ".env.production",
                  ".env.test", ".env.staging", ".envrc"}:
        return {"ok": False,
                "error": f"read denied: '{path}' is a project credential file "
                         "(use .env.example for the documented shape)."}
    return {"ok": True, "path": str(p.resolve())}


def _existing_content(path: str) -> Tuple[Optional[str], bool]:
    """Return (text, exists) for the current file, or (None, exists)."""
    try:
        if Path(path).exists():
            with open(path, "rb") as fh:
                raw = fh.read()
            return raw.decode("utf-8", errors="replace"), True
        return None, False
    except OSError:
        return None, False


def guard_write(path: str, content, max_bytes: int = DEFAULT_WRITE_MAX_BYTES) -> dict:
    """Guard a file write: deny-list, containment, size caps, existing diff.

    Order mirrors Hermes: path deny-list first (file_safety.py
    is_write_denied), then size cap, then — for an existing file — a
    diff of what the write would change (file_state.py staleness check +
    difflib against the current content). Returns
    {'ok': True, 'reason': 'new file'|'no change'|'changed N lines', ...}
    or {'ok': False, 'error': ...} with reason populated.
    """
    p = Path(path).expanduser()
    deny = get_write_denied_error(str(p))
    if deny:
        return {"ok": False, "error": deny, "reason": "denied-path"}

    if max_bytes > 0:
        size = len(content) if isinstance(content, (bytes, bytearray)) else len(str(content))
        if size > max_bytes:
            return {"ok": False,
                    "error": f"write denied: {path} is {size:,} bytes "
                             f"(cap {max_bytes:,})",
                    "reason": "size-cap"}

    before, exists = _existing_content(str(p))
    if not exists:
        return {"ok": True, "reason": "new file", "path": str(p.resolve())}

    new_text = content if isinstance(content, str) else str(content)
    if before == new_text:
        return {"ok": True, "reason": "no change", "path": str(p.resolve())}

    added = sum(1 for line in new_text.split("\n")
                if line not in before.split("\n")) if before is not None else 0
    removed = sum(1 for line in before.split("\n")
                  if line not in new_text.split("\n")) if before is not None else 0
    reason = f"changed {max(added, 1)}/+{added} -{removed} lines"
    return {"ok": True, "reason": reason, "path": str(p.resolve())}


def unified_diff(before: str, after: str, path: str = "file") -> str:
    """Unified diff between two strings (difflib). Empty when identical."""
    if before == after:
        return ""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def record_diff(path: str, before: str, after: str) -> dict:
    """Record a file diff for later retrieval (append-only JSONL log).

    Unlike file_state.py (in-memory only), the diff log is durable so the
    CLI can answer "what changed here?" after a crash. Log location:
    <app home>/diff_log.jsonl. Returns {'ok': True, 'changed': bool,
    'diff': str} — never raises.
    """
    if not isinstance(before, str):
        before = str(before)
    if not isinstance(after, str):
        after = str(after)
    diff = unified_diff(before, after, path)
    if not diff:
        return {"ok": True, "changed": False, "diff": ""}
    try:
        home = _home_dirs()[0]
        log = home / "diff_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "path": str(Path(path).resolve()),
                 "diff": diff}
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # best-effort: the caller still gets the diff in the reply
    return {"ok": True, "changed": True, "diff": diff}


def get_diff_log(limit: int = 20) -> list:
    """Most recent recorded diffs (newest first). For review surfaces."""
    try:
        home = _home_dirs()[0]
        log = home / "diff_log.jsonl"
        if not log.exists():
            return []
        entries = []
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return list(reversed(entries))[:limit]
    except OSError:
        return []


# ── diff/staleness tracking (file_state.py) ─────────────────────────────
ReadStamp = Tuple[float, float, bool]
_MAX_PATHS_PER_AGENT = 4096
_MAX_GLOBAL_WRITERS = 4096


class FileStateRegistry:
    """Process-wide coordinator for cross-agent file edits (file_state.py)."""

    def __init__(self) -> None:
        self._reads: Dict[str, Dict[str, ReadStamp]] = defaultdict(dict)
        self._last_writer: Dict[str, Tuple[str, float]] = {}
        self._path_locks: Dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._state_lock = threading.Lock()

    def _lock_for(self, resolved: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._path_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[resolved] = lock
            return lock

    @contextmanager
    def lock_path(self, resolved: str):
        """Acquire the per-path lock for a read→modify→write section."""
        lock = self._lock_for(resolved)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def record_read(
        self, task_id: str, resolved: str, *, partial: bool = False,
        mtime: Optional[float] = None,
    ) -> None:
        if _disabled():
            return
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        now = time.time()
        with self._state_lock:
            agent_reads = self._reads[task_id]
            agent_reads[resolved] = (float(mtime), now, bool(partial))
            _cap_dict(agent_reads, _MAX_PATHS_PER_AGENT)

    def note_write(
        self, task_id: str, resolved: str, *, mtime: Optional[float] = None,
    ) -> None:
        """Record a successful write (a write is an implicit read)."""
        if _disabled():
            return
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        now = time.time()
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            _cap_dict(self._last_writer, _MAX_GLOBAL_WRITERS)
            self._reads[task_id][resolved] = (float(mtime), now, False)
            _cap_dict(self._reads[task_id], _MAX_PATHS_PER_AGENT)

    def check_stale(self, task_id: str, resolved: str) -> Optional[str]:
        """Return a warning if this write would be stale, else None.

        Staleness classes in order of severity: sibling subagent wrote
        after this agent's last read; mtime drifted from our last read;
        partial (windowed) read; write-without-read.
        """
        if _disabled():
            return None
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)

        if stamp is None and last_writer is None:
            return None

        try:
            current_mtime = os.path.getmtime(resolved)
        except OSError:
            return None  # file doesn't exist — write will create it

        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} but this agent never read it. "
                        "Read the file before writing to avoid overwriting "
                        "the sibling's changes."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                        f"this agent's last read at {_fmt_ts(read_ts)}. "
                        "Re-read the file before writing."
                    )

        if stamp is not None:
            read_mtime, _read_ts, partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"{resolved} was modified since you last read it "
                    "on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing."
                )
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Re-read the whole file before "
                    "overwriting it."
                )

        if stamp is None:
            return (
                f"{resolved} was not read by this agent. "
                "Read the file first so you can write an informed edit."
            )
        return None

    def writes_since(
        self, exclude_task_id: str, since_ts: float, paths: Iterable[str],
    ) -> Dict[str, List[str]]:
        """{writer_task_id: [paths]} for writes after since_ts by others."""
        if _disabled():
            return {}
        paths_set = set(paths)
        out: Dict[str, List[str]] = defaultdict(list)
        with self._state_lock:
            for p, (writer_tid, ts) in self._last_writer.items():
                if writer_tid == exclude_task_id:
                    continue
                if ts < since_ts:
                    continue
                if p in paths_set:
                    out[writer_tid].append(p)
        return dict(out)

    def known_reads(self, task_id: str) -> List[str]:
        if _disabled():
            return []
        with self._state_lock:
            return list(self._reads.get(task_id, {}).keys())

    def clear(self) -> None:
        """Reset all state. Intended for tests only."""
        with self._state_lock:
            self._reads.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()


_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


def _disabled() -> bool:
    return os.environ.get("HERMES_DISABLE_FILE_STATE_GUARD", "").strip() == "1"


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _cap_dict(d: dict, limit: int) -> None:
    """Trim a dict to ``limit`` entries by dropping insertion-order oldest."""
    over = len(d) - limit
    if over <= 0:
        return
    it = iter(d)
    for _ in range(over):
        try:
            d.pop(next(it))
        except (StopIteration, KeyError):
            break


def record_read(task_id: str, resolved_or_path, *, partial: bool = False) -> None:
    _registry.record_read(task_id, str(resolved_or_path), partial=partial)


def note_write(task_id: str, resolved_or_path) -> None:
    _registry.note_write(task_id, str(resolved_or_path))


def check_stale(task_id: str, resolved_or_path) -> Optional[str]:
    return _registry.check_stale(task_id, str(resolved_or_path))


def lock_path(resolved_or_path):
    return _registry.lock_path(str(resolved_or_path))


def writes_since(exclude_task_id: str, since_ts: float, paths) -> Dict[str, List[str]]:
    return _registry.writes_since(exclude_task_id, since_ts, [str(p) for p in paths])


def known_reads(task_id: str) -> List[str]:
    return _registry.known_reads(task_id)


# ── write-approval staging (write_approval.py) ──────────────────────────
def write_approval_enabled(subsystem: str) -> bool:
    """Return whether the approval gate is enabled for ``subsystem``.

    Reads ``safety.write_approval`` from core.settings (Atropos has a
    single gate, not per-subsystem keys — see module docstring). Defaults
    to False for any unset/invalid value.
    """
    if subsystem not in _SUBSYSTEMS:
        return False
    try:
        from . import settings as _s
        raw = _s.get("safety.write_approval", False)
    except Exception:
        return False
    return _normalize_enabled(raw)


def _normalize_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"on", "true", "yes", "1", "approve", "enabled"}
    return False


def _pending_dir(subsystem: str) -> Path:
    home = _home_dirs()[0]
    return home / "pending" / subsystem


def stage_write(subsystem: str, payload: Dict[str, Any],
                *, summary: str, origin: str) -> Dict[str, Any]:
    """Persist a pending write; return the record (best-effort)."""
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": subsystem,
        "action": payload.get("action", ""),
        "summary": (summary or "").strip(),
        "origin": origin or "foreground",
        "created_at": time.time(),
        "payload": payload,
    }
    try:
        d = _pending_dir(subsystem)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{pid}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # safe failure: nothing silently committed
    return record


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """All pending records for ``subsystem``, oldest first."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def discard_pending(subsystem: str, pending_id: str) -> bool:
    """Delete a pending record. Returns True if it existed."""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def pending_count(subsystem: str) -> int:
    """Cheap count of pending records (for notification badges)."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except OSError:
        return 0


def evaluate_gate(subsystem: str, *, inline_summary: str = "",
                  inline_detail: str = "") -> Dict[str, Any]:
    """Decide what to do with a pending write for ``subsystem``.

    Decision matrix (write_approval.py): gate off → allow; gate on →
    stage (Atropos has no interactive approval callback, so memory
    foreground writes stage like every other surface). Returns a dict
    with exactly one of allow/stage set:
    {'allow': True} or {'staged': True, 'message': str}.
    """
    if not write_approval_enabled(subsystem):
        return {"allow": True}
    where = "/skills pending" if subsystem == SKILLS else "/memory pending"
    return {
        "staged": True,
        "message": (
            f"Staged for approval ({subsystem}.write_approval is on). "
            f"Not yet saved — review with {where}."
        ),
    }


def skill_gist(action: str, name: str, *, content: str = "",
               file_path: str = "", old_string: str = "",
               new_string: str = "") -> str:
    """One-line human gist for a pending skill write (write_approval.py)."""
    import re
    if action in {"create", "edit"} and content:
        desc = ""
        m = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
        if m:
            desc = m.group(1).strip().strip("'\"")[:140]
        size = f"{len(content) // 1024 + 1} KB" if len(content) >= 1024 else f"{len(content)} chars"
        verb = "create" if action == "create" else "rewrite"
        if desc:
            return f"{verb} '{name}' — {desc} ({size})"
        return f"{verb} '{name}' ({size})"
    if action == "patch":
        target = file_path or "SKILL.md"
        removed = old_string.count("\n") + 1 if old_string else 0
        added = new_string.count("\n") + 1 if new_string else 0
        return f"patch '{name}' {target} (+{added}/-{removed} lines)"
    if action == "write_file":
        return f"write {file_path} in '{name}'"
    if action == "remove_file":
        return f"remove {file_path} from '{name}'"
    if action == "delete":
        return f"delete skill '{name}'"
    return f"{action} '{name}'"


# ── working-tree diff (working_diff.py) ─────────────────────────────────
_GIT_TIMEOUT = 15
_MAX_UNTRACKED_FILES = 50
VALID_MODES = ("working", "staged", "all")


def _run(args: List[str], cwd: str, timeout: int = _GIT_TIMEOUT):
    """Run git, returning (returncode, stdout). Never raises on git failure."""
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout


def _untracked_files(cwd: str) -> List[str]:
    code, out = _run(["ls-files", "--others", "--exclude-standard"], cwd)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def _untracked_diff(cwd: str, files: List[str]) -> str:
    """Render untracked files as new-file diffs via ``git diff --no-index``."""
    chunks: List[str] = []
    for rel in files[:_MAX_UNTRACKED_FILES]:
        try:
            # --no-index exits 1 when the files differ — the success path.
            _, out = _run(["diff", "--no-index", "--", os.devnull, rel], cwd)
            if out.strip():
                chunks.append(out.rstrip("\n"))
        except (subprocess.TimeoutExpired, OSError):
            continue
    if len(files) > _MAX_UNTRACKED_FILES:
        chunks.append(
            f"... ({len(files) - _MAX_UNTRACKED_FILES} more untracked files not shown)"
        )
    return "\n".join(chunks)


def collect_working_diff(cwd: str, mode: str = "working",
                         paths: Optional[List[str]] = None) -> Dict:
    """Collect a git diff of the working directory.

    Returns ``{"success", "stat", "diff", "untracked", "empty"}`` on
    success or ``{"success": False, "error": ...}`` when git is
    unavailable / not a repo / timed out.
    """
    if mode not in VALID_MODES:
        return {"success": False,
                "error": f"Unknown mode '{mode}'. Use: {', '.join(VALID_MODES)}"}

    if not shutil.which("git"):
        return {"success": False, "error": "git is not installed or not on PATH."}

    try:
        code, _ = _run(["rev-parse", "--is-inside-work-tree"], cwd, timeout=5)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"git failed: {e}"}
    if code != 0:
        return {"success": False, "error": "Not a git repository."}

    if mode == "staged":
        base_args = ["diff", "--cached"]
    elif mode == "all":
        base_args = ["diff", "HEAD"]
    else:  # working
        base_args = ["diff"]

    pathspec = ["--", *paths] if paths else []

    try:
        _, stat_out = _run([*base_args, "--stat", *pathspec], cwd)
        _, diff_out = _run([*base_args, *pathspec], cwd, timeout=_GIT_TIMEOUT * 2)

        untracked: List[str] = []
        untracked_diff = ""
        if mode in ("working", "all") and not paths:
            untracked = _untracked_files(cwd)
            if untracked:
                untracked_diff = _untracked_diff(cwd, untracked)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "git diff timed out."}
    except OSError as e:
        return {"success": False, "error": f"git failed: {e}"}

    stat = stat_out.strip()
    diff = diff_out.strip()
    if untracked_diff:
        diff = f"{diff}\n{untracked_diff}".strip()

    result = {
        "success": True,
        "stat": stat,
        "diff": diff,
        "untracked": untracked,
    }
    if not stat and not diff and not untracked:
        result["empty"] = True
    return result
