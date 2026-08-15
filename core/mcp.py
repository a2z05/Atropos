#!/usr/bin/env python3
"""Atropos universal MCP registry — one list over every harness's servers.

MCP (Model Context Protocol) servers are currently configured per-harness:
Claude Code reads ``~/.claude.json`` / ``~/.claude/mcp.json``, Hermes reads
``config.yaml``. This module keeps a single canonical registry at
``~/.atropos/mcp_servers.json`` that can:

  * discover servers from Claude + Hermes configs (``rescan``) and import
    the ones Atropos does not know yet (dedupe by name, ask-first via
    ``adopt``),
  * add / remove / enable / disable servers with safe name validation,
  * probe server health without ever crashing (``status`` / ``test``),
  * project one server entry back into a harness config (``project_to_harness``)
    with secret values replaced by ``{ref:secrets.json:<key>}`` placeholders —
    plaintext tokens are never written to harness configs.

Canonical entry shape::

    {
      "name": "github",
      "type": "stdio" | "http",
      "command": "npx",            # stdio servers only
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "..."},
      "enabled": true,
      "source": "hermes" | "claude" | "manual",
      "mode": "shared" | "per-harness" | "atropos-only",
      "adopted": false,            # imported by rescan but not yet adopted
      "url": "https://..."         # http servers only
    }

Pure stdlib. Never imports core.dashboard (circular).
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, detect

# ── identifiers / constants ───────────────────────────────────────────────
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

TYPES = ("stdio", "http")
MODES = ("shared", "per-harness", "atropos-only")
SOURCES = ("hermes", "claude", "manual")

# keys scanned inside hermes config.yaml for server definitions
HERMES_KEYS = ("mcp", "mcp_servers", "plugins")

# Value shapes that look like live secrets and must be placeholder-ized
# before being written into a harness config.
_PLACEHOLDER_RE = re.compile(r"^\{ref:[^{}]+\}$")
_SECRET_KEYS = re.compile(r"(token|secret|key|password|credential|auth)", re.IGNORECASE)


def valid_name(name: str) -> bool:
    """True when ``name`` is a safe MCP server identifier (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name))


def registry_path() -> Path:
    """Canonical registry file (~/.atropos/mcp_servers.json)."""
    return detect.atropos_home() / "mcp_servers.json"


def _load() -> list:
    """Load registry entries; corrupt/missing files yield an empty list."""
    p = registry_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _save(entries: list):
    """Write registry entries, creating ~/.atropos on demand."""
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _entry(entries: list, name: str) -> dict | None:
    """Find a registry entry by name."""
    for e in entries:
        if e.get("name") == name:
            return e
    return None


def trash_dir() -> Path:
    """Trash root for removed server entries (reversible)."""
    d = detect.atropos_home() / "trash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    """UTC timestamp for records."""
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


# ── discovery ─────────────────────────────────────────────────────────────
def _discover_claude() -> dict:
    """Parse Claude's mcpServers from ~/.claude.json and ~/.claude/mcp.json.

    Returns a dict name → {type, command, args, env, url, source:'claude'}.
    Both files may carry ``mcpServers`` mappings; ``.claude.json`` wins on
    name clashes (it is the live user config).
    """
    out = {}
    home = detect._home()
    files = []
    claude_json = home / ".claude.json"
    if claude_json.exists():
        files.append(claude_json)
    mcp_json = home / ".claude" / "mcp.json"
    if mcp_json.exists():
        files.append(mcp_json)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        if not isinstance(servers, dict):
            continue
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                continue
            entry = _normalize_discovered(name, spec, "claude")
            if entry:
                out[name] = entry
    return out


def _discover_hermes() -> dict:
    """Parse Hermes config.yaml for MCP server definitions.

    Looked up under the keys ``mcp``, ``mcp_servers`` and ``plugins``.
    Returns a dict name → normalized entry (source 'hermes').
    """
    out = {}
    cfg_path = detect.hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return out
    try:
        data = config.parse_yaml(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for key in HERMES_KEYS:
        node = data.get(key)
        if isinstance(node, dict) and "servers" in node:
            node = node["servers"]
        if not isinstance(node, dict):
            continue
        for name, spec in node.items():
            if not isinstance(spec, dict):
                continue
            entry = _normalize_discovered(name, spec, "hermes")
            if entry:
                out[name] = entry
    return out


def _normalize_discovered(name: str, spec: dict, source: str) -> dict | None:
    """Build a registry entry from a raw harness server spec (or None)."""
    if not valid_name(name):
        return None
    cmd = spec.get("command") if isinstance(spec.get("command"), str) else ""
    args = spec.get("args")
    if args is None:
        args = []
    if not isinstance(args, list):
        args = []
    args = [str(a) for a in args]
    env = spec.get("env")
    if not isinstance(env, dict):
        env = {}
    env = {str(k): str(v) for k, v in env.items() if v is not None}
    url = spec.get("url") or spec.get("base_url") or ""
    if url and not isinstance(url, str):
        url = str(url)
    if url:
        stype = "http"
    elif cmd:
        stype = "stdio"
    else:
        return None
    return {
        "name": name,
        "type": stype,
        "command": cmd if stype == "stdio" else "",
        "args": args,
        "env": env,
        "url": url,
        "enabled": bool(spec.get("enabled", True)),
        "source": source,
        "mode": "shared",
        "adopted": False,
    }


def rescan() -> dict:
    """Import harness servers not already in the registry.

    Returns ``{"found": [...], "added": [...], "skipped": [...]}`` where
    ``found`` lists every discovered name, ``added`` the names newly
    imported (``adopted=False`` — the ask-first gate) and ``skipped`` the
    names already present in the registry. Never crashes on unreadable
    configs.
    """
    discovered = {}
    for src in (_discover_claude(), _discover_hermes()):
        for name, entry in src.items():
            if name not in discovered:
                discovered[name] = entry
    entries = _load()
    existing = {e["name"] for e in entries}
    found = sorted(discovered)
    added, skipped = [], []
    for name in found:
        if name in existing:
            skipped.append(name)
            continue
        entries.append(discovered[name])
        added.append(name)
    if added:
        _save(entries)
    return {"found": found, "added": added, "skipped": skipped}


def adopt(server_names: list | None = None) -> dict:
    """Mark discovered servers as adopted (the ask-first gate).

    ``server_names`` may be a list of names or None/``"all"`` for every
    unadopted server. Returns {"adopted": [...], "skipped": [...]}.
    """
    entries = _load()
    if not server_names or server_names == "all":
        targets = [e["name"] for e in entries if not e.get("adopted")]
    else:
        targets = list(server_names)
    adopted, skipped = [], []
    changed = False
    for name in targets:
        if not valid_name(name):
            skipped.append(name)
            continue
        e = _entry(entries, name)
        if e is None:
            skipped.append(name)
            continue
        if e.get("adopted"):
            skipped.append(name)
            continue
        e["adopted"] = True
        adopted.append(name)
        changed = True
    if changed:
        _save(entries)
    return {"adopted": adopted, "skipped": skipped}


# ── registry CRUD ─────────────────────────────────────────────────────────
def list_servers() -> list:
    """All registry entries with their current probe status."""
    out = []
    for e in _load():
        row = dict(e)
        st = status(e["name"], timeout=2)
        row["status"] = st
        out.append(row)
    return out


def add(name: str, type_: str = "stdio", command: str = "", args: list | None = None,
        env: dict | None = None, url: str = "", mode: str = "shared") -> dict:
    """Register a new MCP server manually.

    ``type_`` is ``stdio`` (needs ``command``) or ``http`` (needs ``url``).
    Raises ValueError on invalid names/types/modes or duplicate names.
    """
    if not valid_name(name):
        raise ValueError(f"invalid server name: {name!r}")
    if type_ not in TYPES:
        raise ValueError(f"type must be one of: {', '.join(TYPES)}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    if type_ == "stdio":
        if not command or not isinstance(command, str):
            raise ValueError("stdio servers need a command")
    else:
        if not url or not str(url).startswith(("http://", "https://")):
            raise ValueError("http servers need an http(s) url")
    entries = _load()
    if _entry(entries, name) is not None:
        raise ValueError(f"server already registered: {name}")
    if args is None:
        args = []
    if env is None:
        env = {}
    entry = {
        "name": name,
        "type": type_,
        "command": command if type_ == "stdio" else "",
        "args": [str(a) for a in args],
        "env": {str(k): str(v) for k, v in env.items() if v is not None},
        "url": url if type_ == "http" else "",
        "enabled": True,
        "source": "manual",
        "mode": mode,
        "adopted": True,
    }
    entries.append(entry)
    _save(entries)
    return entry


def remove(name: str) -> dict:
    """Remove a server — the JSON entry is trashed (reversible copy)."""
    if not valid_name(name):
        raise ValueError(f"invalid server name: {name!r}")
    entries = _load()
    e = _entry(entries, name)
    if e is None:
        raise FileNotFoundError(f"server not found: {name}")
    entries = [x for x in entries if x.get("name") != name]
    _save(entries)
    stamp = _now()
    dest = trash_dir() / f"mcp-{name}-{stamp}.json"
    dest.write_text(json.dumps(e, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "name": name, "trashed": str(dest)}


def enable(name: str) -> dict:
    """Enable a server."""
    return _set_enabled(name, True)


def disable(name: str) -> dict:
    """Disable a server (probes and projections skip it)."""
    return _set_enabled(name, False)


def _set_enabled(name: str, value: bool) -> dict:
    if not valid_name(name):
        raise ValueError(f"invalid server name: {name!r}")
    entries = _load()
    e = _entry(entries, name)
    if e is None:
        raise FileNotFoundError(f"server not found: {name}")
    e["enabled"] = value
    _save(entries)
    return {"ok": True, "name": name, "enabled": value}


def mode(name: str, mode_: str) -> dict:
    """Set the deployment mode of a server (shared/per-harness/atropos-only)."""
    if not valid_name(name):
        raise ValueError(f"invalid server name: {name!r}")
    if mode_ not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    entries = _load()
    e = _entry(entries, name)
    if e is None:
        raise FileNotFoundError(f"server not found: {name}")
    e["mode"] = mode_
    _save(entries)
    return {"ok": True, "name": name, "mode": mode_}


# ── health probes ─────────────────────────────────────────────────────────
def status(name: str, timeout: float = 3) -> dict:
    """Probe one server. Never raises — failures are recorded in the result.

    stdio servers: spawn ``command --version`` (2s timeout, shell off).
    http servers: GET the url (or base url) with a 3s timeout; ok when the
    HTTP status is < 500.
    """
    if not valid_name(name):
        return {"name": name, "ok": False, "error": "invalid name", "latency_ms": None}
    entries = _load()
    e = _entry(entries, name)
    if e is None:
        return {"name": name, "ok": False, "error": "not registered", "latency_ms": None}
    if not e.get("enabled"):
        return {"name": name, "ok": False, "error": "disabled", "latency_ms": None}
    t0 = time.monotonic()
    if e.get("type") == "http":
        return _probe_http(e, timeout, t0)
    return _probe_stdio(e, t0)


def _probe_http(e: dict, timeout: float, t0: float) -> dict:
    url = str(e.get("url") or "").rstrip("/")
    if not url:
        return {"name": e["name"], "ok": False, "error": "no url", "latency_ms": None}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = resp.status < 500
            return {
                "name": e["name"],
                "ok": ok,
                "status_code": resp.status,
                "error": None if ok else f"HTTP {resp.status}",
                "latency_ms": round((time.monotonic() - t0) * 1000),
            }
    except urllib.error.HTTPError as err:
        try:
            code = err.code
            err.close()  # release the response body file
        except Exception:
            code = getattr(err, "code", None)
        ok = code < 500
        return {
            "name": e["name"],
            "ok": ok,
            "status_code": code,
            "error": None if ok else f"HTTP {code}",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:
        return {"name": e["name"], "ok": False, "error": str(exc), "latency_ms": None}


def _probe_stdio(e: dict, t0: float) -> dict:
    command = e.get("command") or ""
    if not command:
        return {"name": e["name"], "ok": False, "error": "no command", "latency_ms": None}
    argv = [command] + [str(a) for a in (e.get("args") or [])]
    probe = argv + ["--version"]
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            shell=False,
        )
        ok = result.returncode == 0
        return {
            "name": e["name"],
            "ok": ok,
            "error": None if ok else f"exit {result.returncode}",
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"name": e["name"], "ok": False, "error": "timeout",
                "latency_ms": round((time.monotonic() - t0) * 1000)}
    except Exception as exc:
        return {"name": e["name"], "ok": False, "error": str(exc),
                "latency_ms": round((time.monotonic() - t0) * 1000)}


def test(name: str) -> dict:
    """Alias for status() with the default 3s timeout."""
    return status(name, timeout=3)


# ── projection into harnesses ─────────────────────────────────────────────
def _looks_like_secret(key: str, value: str) -> bool:
    """A value is secret when its key smells like a credential and the
    value is a plausible live token (>= 8 chars, not already a
    ``{ref:...}`` placeholder, not obviously a default/empty string)."""
    if not value or len(value) < 8:
        return False
    if _PLACEHOLDER_RE.match(value):
        return False
    if value in ("", "null", "none", "true", "false"):
        return False
    return bool(_SECRET_KEYS.search(key))


def _secret_placeholder(key: str, value: str) -> str:
    """Build the placeholder referencing the shared secrets store.

    ``{ref:secrets.json:<key>}`` — the projection consumer substitutes the
    real value from ``~/.atropos/secrets.json`` at runtime.
    """
    return f"{{ref:secrets.json:{key}}}"


def _project_entry(e: dict) -> dict:
    """Registry entry → harness server spec with secrets placeholder-ized."""
    spec: dict = {}
    if e.get("type") == "http":
        spec["url"] = e.get("url") or ""
        spec["type"] = "http"
    else:
        spec["command"] = e.get("command") or ""
        if e.get("args"):
            spec["args"] = list(e["args"])
    env = {}
    for k, v in (e.get("env") or {}).items():
        if isinstance(v, str) and _looks_like_secret(k, v):
            env[k] = _secret_placeholder(k, v)
        else:
            env[k] = v
    if env:
        spec["env"] = env
    if e.get("enabled") is False:
        spec["enabled"] = False
    return spec


def project_to_harness(name: str, harness: str) -> dict:
    """Write one server into a harness config (claude | hermes).

    Claude: merge into ``~/.claude/mcp.json`` ``mcpServers`` (created on
    demand). Hermes: merge into the ``mcp:`` section of hermes
    ``config.yaml``. Secret env values become
    ``{ref:secrets.json:<key>}`` placeholders — never plaintext tokens.
    """
    if harness not in ("claude", "hermes"):
        raise ValueError("harness must be 'claude' or 'hermes'")
    if not valid_name(name):
        raise ValueError(f"invalid server name: {name!r}")
    entries = _load()
    e = _entry(entries, name)
    if e is None:
        raise FileNotFoundError(f"server not found: {name}")
    spec = _project_entry(e)
    if harness == "claude":
        return _project_to_claude(name, spec)
    return _project_to_hermes(name, spec)


def _project_to_claude(name: str, spec: dict) -> dict:
    mcp_path = detect._home() / ".claude" / "mcp.json"
    data = {}
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    servers[name] = spec
    data["mcpServers"] = servers
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "harness": "claude", "name": name, "path": str(mcp_path)}


def _project_to_hermes(name: str, spec: dict) -> dict:
    cfg_path = detect.hermes_home() / "config.yaml"
    data = {}
    if cfg_path.exists():
        try:
            data = config.parse_yaml(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
    servers = mcp.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = spec
    mcp["servers"] = servers
    data["mcp"] = mcp
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(config.dump_yaml(data) + "\n", encoding="utf-8")
    return {"ok": True, "harness": "hermes", "name": name, "path": str(cfg_path)}


# ── stats ─────────────────────────────────────────────────────────────────
def stats() -> dict:
    """Registry summary: total, enabled, per_source, per_mode."""
    entries = _load()
    per_source = {}
    per_mode = {}
    for e in entries:
        src = e.get("source", "manual")
        md = e.get("mode", "shared")
        per_source[src] = per_source.get(src, 0) + 1
        per_mode[md] = per_mode.get(md, 0) + 1
    return {
        "total": len(entries),
        "enabled": sum(1 for e in entries if e.get("enabled")),
        "per_source": per_source,
        "per_mode": per_mode,
    }


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args and args[0] == "rescan":
        print(json.dumps(rescan(), indent=2, ensure_ascii=False))
    else:
        for e in list_servers():
            state = "on" if e.get("enabled") else "off"
            st = e.get("status", {})
            mark = "OK" if st.get("ok") else "FAIL"
            print(f"  [{state}] {e['name']:<24} {e.get('type'):<5} {e.get('source'):<7} "
                  f"adopted={e.get('adopted')} [{mark}] {st.get('error') or ''}")
