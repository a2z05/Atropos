#!/usr/bin/env python3
"""Atropos dashboard — tiny stdlib HTTP server (http.server), no deps.

Serves dashboard/index.html + /api/* JSON endpoints. Auth via token in
~/.atropos/auth_token (X-Atropos-Token header, or ?token= for GET).

The API is a superset of the Hermes web-dashboard surface: sessions, models,
logs, cron, skills, plugins, channels, config, analytics, files, backup,
history — plus Atropos-specific controls (doctor, patches, routers, guest,
update).
"""
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config, detect, doctor, guest, patches, router, update

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
REPO_DIR = Path(__file__).resolve().parent.parent


# ── auth ─────────────────────────────────────────────────────────────────
def _auth_token() -> str:
    p = detect.atropos_home() / "auth_token"
    if p.exists():
        return p.read_text().strip()
    token = secrets.token_urlsafe(16)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n")
    return token


def _body(status, data):
    return json.dumps(data, ensure_ascii=False).encode(), "application/json", status


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── history (dashboard action log) ───────────────────────────────────────
def history_log(action: str, detail=""):
    """Append one action to ~/.atropos/history.jsonl."""
    try:
        p = detect.atropos_home() / "history.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _ts(), "action": action, "detail": detail}) + "\n")
    except Exception:
        pass


def history_list(limit=50):
    p = detect.atropos_home() / "history.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out[::-1]


# ── version / self-update detection ──────────────────────────────────────
def _version() -> str:
    p = REPO_DIR / "VERSION"
    try:
        return p.read_text().strip()
    except Exception:
        return "1.0.0"


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or ""
    except Exception:
        return ""


def api_version():
    return {
        "ok": True,
        "version": _version(),
        "sha": _git_sha(),
    }


# ── disk ─────────────────────────────────────────────────────────────────
def _disk():
    try:
        usage = shutil.disk_usage(str(detect.hermes_home()))
        pct = (usage.used / usage.total) * 100
        return round(pct, 1), round(usage.total / 2**30, 1), round(usage.used / 2**30, 1)
    except Exception:
        return None, None, None


# ── Hermes dashboard mirror surface ──────────────────────────────────────
def _find_state_db() -> Path:
    """Locate hermes state.db (sessions/analytics live here)."""
    home = detect.hermes_home()
    candidates = [
        home / "state.db",
        home / "data" / "state.db",
        home / "state" / "state.db",
        home / "db" / "state.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    # recursive search bounded
    try:
        hits = list(home.glob("**/state.db"))[:5]
        if hits:
            return hits[0]
    except Exception:
        pass
    return candidates[0]


def _sql_scalar(query: str, default=0):
    db = _find_state_db()
    if not db.exists():
        return default
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cur = con.cursor()
            cur.execute(query)
            row = cur.fetchone()
            return row[0] if row else default
        finally:
            con.close()
    except Exception:
        return default


def _sql_rows(query: str, limit=100):
    db = _find_state_db()
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cur = con.cursor()
            cur.execute(query)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(limit)
            return [dict(zip(cols, r)) for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def api_sessions():
    """Sessions: total count, active count, recent titles."""
    db = _find_state_db()
    present = db.exists()
    tables = []
    if present:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            finally:
                con.close()
        except Exception:
            tables = []
    session_tbl = None
    for name in tables:
        if "session" in name.lower():
            session_tbl = name
            break
    recent = []
    total = 0
    active = 0
    if session_tbl:
        try:
            recent = _sql_rows(
                f"SELECT * FROM {session_tbl} ORDER BY rowid DESC LIMIT 15", limit=15)
            total = _sql_scalar(f"SELECT COUNT(*) FROM {session_tbl}")
            # try common active-column names
            for col in ("active", "status", "is_active"):
                try:
                    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
                    try:
                        cols = [d[1] for d in con.execute(f"PRAGMA table_info({session_tbl})")]
                        if col in cols:
                            active = con.execute(
                                f"SELECT COUNT(*) FROM {session_tbl} WHERE {col}=1").fetchone()[0]
                            break
                    finally:
                        con.close()
                except Exception:
                    continue
        except Exception:
            recent = []
    return {
        "ok": True,
        "db": str(db),
        "present": present,
        "table": session_tbl,
        "tables": tables,
        "total": total,
        "active": active,
        "recent": recent,
    }


def api_models():
    """Current default model + provider."""
    cfg = config.load()
    r = cfg.get("router", {})
    env_model = os.environ.get("DEFAULT_MODEL", "")
    base = r.get("base_url", "") or os.environ.get("OPENAI_BASE_URL", "")
    return {
        "ok": True,
        "model": r.get("model", env_model or "deepmo"),
        "provider": r.get("active", "nain"),
        "base_url": base,
        "env": {
            "DEFAULT_MODEL": env_model or "",
            "OPENAI_BASE_URL": base,
        },
    }


def _yaml_files(root: Path):
    if not root.exists():
        return []
    return sorted([p for p in root.glob("*.yaml")] + [p for p in root.glob("*.yml")])


def api_cron():
    """Cron jobs from $HERMES_HOME/cron/*.yaml."""
    cron_dir = detect.hermes_home() / "cron"
    jobs = []
    for f in _yaml_files(cron_dir):
        try:
            data = config.parse_yaml(f.read_text(encoding="utf-8"))
            jobs.append({
                "name": f.stem,
                "file": f.name,
                "schedule": data.get("schedule", data.get("cron", "")),
                "command": data.get("command", data.get("task", "")),
                "enabled": data.get("enabled", True),
            })
        except Exception as e:
            jobs.append({"name": f.stem, "file": f.name, "error": str(e)})
    return {"ok": True, "dir": str(cron_dir), "exists": cron_dir.exists(), "jobs": jobs}


def api_skills():
    """Skills from $HERMES_HOME/skills/*/SKILL.md (and ~/.claude/skills)."""
    dirs = [detect.hermes_home() / "skills", detect._home() / ".claude" / "skills"]
    skills = []
    for d in dirs:
        if not d.exists():
            continue
        for sk in sorted(d.iterdir()):
            md = sk / "SKILL.md"
            if md.exists():
                try:
                    head = md.read_text(encoding="utf-8", errors="replace")[:240]
                except Exception:
                    head = ""
                skills.append({"name": sk.name, "path": str(md), "source": str(d), "head": head})
    return {"ok": True, "skills": skills}


def api_plugins():
    """Plugins from $HERMES_HOME/plugins (dirs with __init__.py or .py)."""
    plugin_dir = detect.hermes_home() / "plugins"
    plugins = []
    if plugin_dir.exists():
        for p in sorted(plugin_dir.iterdir()):
            if p.is_dir():
                plugins.append({"name": p.name, "type": "dir", "path": str(p)})
            elif p.suffix == ".py":
                plugins.append({"name": p.stem, "type": "py", "path": str(p)})
    return {"ok": True, "dir": str(plugin_dir), "exists": plugin_dir.exists(), "plugins": plugins}


def _env_bool(key: str) -> bool:
    v = os.environ.get(key, "")
    return bool(v and v.lower() not in ("0", "false", "no", ""))


def api_channels():
    """Platform config: presence of tokens for telegram/discord/slack."""
    channels = [
        {
            "name": "telegram",
            "configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN") or _env_bool("TELEGRAM_ENABLED")),
            "token_env": "TELEGRAM_BOT_TOKEN",
            "detail": "token set" if os.environ.get("TELEGRAM_BOT_TOKEN") else "missing",
        },
        {
            "name": "discord",
            "configured": bool(os.environ.get("DISCORD_TOKEN") or _env_bool("DISCORD_ENABLED")),
            "token_env": "DISCORD_TOKEN",
            "detail": "token set" if os.environ.get("DISCORD_TOKEN") else "missing",
        },
        {
            "name": "slack",
            "configured": bool(os.environ.get("SLACK_TOKEN") or os.environ.get("SLACK_BOT_TOKEN") or _env_bool("SLACK_ENABLED")),
            "token_env": "SLACK_TOKEN",
            "detail": "token set" if (os.environ.get("SLACK_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")) else "missing",
        },
    ]
    return {"ok": True, "channels": channels}


def api_hermes_config():
    """Show + edit hermes config.yaml."""
    p = detect.hermes_home() / "config.yaml"
    if not p.exists():
        return {"ok": True, "exists": False, "path": str(p), "content": ""}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        parsed = config.parse_yaml(text)
        return {"ok": True, "exists": True, "path": str(p), "content": text, "parsed": parsed}
    except Exception as e:
        return {"ok": True, "exists": True, "path": str(p), "content": "", "error": str(e)}


def api_analytics():
    """Messages / sessions / tokens aggregate."""
    db = _find_state_db()
    present = db.exists()
    tables = []
    if present:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            finally:
                con.close()
        except Exception:
            tables = []
    msgs = _sql_scalar("SELECT COUNT(*) FROM messages", "n/a") if "messages" in tables else "n/a"
    sesses = _sql_scalar("SELECT COUNT(*) FROM sessions", "n/a") if "sessions" in tables else "n/a"
    return {
        "ok": True,
        "messages": msgs,
        "sessions": sesses,
        "tokens": "n/a",
        "db_present": present,
        "tables": tables,
    }


def api_files():
    """Top-level listing of the workspace (repo) dir."""
    root = REPO_DIR
    entries = []
    for p in sorted(root.iterdir()):
        if p.name.startswith(".git"):
            continue
        try:
            st = p.stat()
            entries.append({
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": st.st_size if not p.is_dir() else None,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
        except Exception:
            continue
    return {"ok": True, "root": str(root), "entries": entries}


def _latest_backup():
    backups_dir = detect.atropos_home() / "backups"
    if not backups_dir.exists():
        return None
    items = [p for p in backups_dir.iterdir() if p.is_dir() or p.suffix == ".gz"]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not items:
        return None
    latest = items[0]
    st = latest.stat()
    return {
        "path": str(latest),
        "name": latest.name,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "size": st.st_size if latest.is_file() else None,
    }


def api_backup(trigger=False):
    if trigger:
        bk = update.backup_state(detect.atropos_home())
        history_log("backup", str(bk))
        return {"ok": True, "created": str(bk), "latest": _latest_backup()}
    return {"ok": True, "latest": _latest_backup(), "dir": str(detect.atropos_home() / "backups")}


def api_history(limit=50):
    return {"ok": True, "entries": history_list(limit=limit)}


# ── Atropos-specific ─────────────────────────────────────────────────────
def api_status():
    rt = detect.detect()
    pct, total, used = _disk()
    return {
        "ok": True,
        "runtime": rt,
        "disk": {"pct": pct, "total_gb": total, "used_gb": used},
        "router": router.get(),
        "config": config.load(),
        "ts": _ts(),
    }


def api_doctor(fix=False):
    if fix:
        history_log("doctor", "fix")
    return {"ok": True, "checks": doctor.doctor(fix=fix)}


def api_patches():
    """Patch status + a bounded old/new snippet for expandable diffs."""
    hacks = {h["id"]: h for h in patches.load_hacks()}
    results = patches.verify()
    for r in results:
        h = hacks.get(r["id"], {})
        r["old"] = h.get("old", "")[:2000]
        r["new"] = h.get("new", "")[:2000]
        r["file"] = h.get("_file", "")
    return {"ok": True, "patches": results}


def api_patches_apply():
    """Re-apply all hacks to the hermes-agent tree."""
    applied, skipped, errors = patches.apply_hacks()
    history_log("patches", f"applied={len(applied)} errors={len(errors)}")
    return {"ok": not errors, "applied": applied, "skipped": skipped, "errors": errors}


def api_update(check=True):
    repo = detect.hermes_agent()
    if not repo:
        return {"ok": False, "error": "hermes-agent not found"}
    if check:
        return update.update_check(repo)
    history_log("update", "apply")
    return update.apply_update(repo)


def api_route(action=None, name=None):
    if action == "set" and name:
        try:
            r = router.set_active(name)
            router.apply_all()
            history_log("route", name)
            return {"ok": True, "router": r}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "router": router.get(), "available": router.available()}


def api_route_test(name=None):
    """Live-ping a router (defaults to all available)."""
    names = [name] if name else router.available()
    results = {}
    for n in names:
        results[n] = router.ping(n)
    return {"ok": True, "results": results}


def api_route_apply():
    """Push the active router to Hermes .env + Claude settings.json."""
    results = router.apply_all()
    history_log("route", "applied to hermes+claude")
    return {"ok": True, "results": results}


def api_config_get(key=None):
    if key == "version":
        return {"ok": True, "value": _version()}
    if key is None:
        return {"ok": True, "config": config.load(), "version": _version()}
    val = config.get(key)
    return {"ok": True, "value": val}


def api_config_set(key=None, value=None):
    if not key:
        return {"ok": False, "error": "key required"}
    config.set_path(key, value)
    history_log("config", f"{key}={json.dumps(value, ensure_ascii=False)}")
    return {"ok": True, "key": key, "value": config.get(key)}


def api_guest():
    return {"ok": True, **guest.status()}


def api_guest_toggle():
    st = guest.toggle()
    history_log("guest", "enabled" if st["enabled"] else "disabled")
    return {"ok": True, **st}


def api_guest_persona(method="GET", content=None):
    """Read or write the guest persona file."""
    p = Path(guest._persona_path())
    if method == "POST":
        if content is None:
            return {"ok": False, "error": "content required"}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        history_log("guest", "persona saved")
        return {"ok": True, "saved": str(p)}
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": True, "exists": True, "error": str(e), "content": ""}
        return {"ok": True, "exists": True, "path": str(p), "content": text}
    return {"ok": True, "exists": False, "path": str(p), "content": ""}


def api_claude_settings(content=None):
    """Read or write ~/.claude/settings.json (exposed for the Claude panel)."""
    settings_file = detect._home() / ".claude" / "settings.json"
    if content is not None:
        try:
            parsed = json.loads(content)
        except Exception as e:
            return {"ok": False, "error": f"invalid JSON: {e}"}
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
        history_log("claude", "settings.json saved")
        return {"ok": True, "saved": str(settings_file)}
    if settings_file.exists():
        try:
            text = settings_file.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": True, "exists": True, "error": str(e), "content": ""}
        return {"ok": True, "exists": True, "path": str(settings_file), "content": text}
    return {"ok": True, "exists": False, "path": str(settings_file), "content": ""}


def api_logs(tail=80):
    logs_dir = detect.hermes_home() / "logs"
    if not logs_dir.exists():
        return {"ok": True, "lines": [], "error": "no logs dir"}
    log_files = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not log_files:
        return {"ok": True, "lines": [], "error": "no log files"}
    target = log_files[-1]
    try:
        with open(target, "r", errors="replace") as f:
            all_lines = f.readlines()
        return {"ok": True, "lines": all_lines[-tail:], "file": target.name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_claude():
    """Claude panel: binary version, skills, settings.json, model aliases."""
    bin_path = detect._find_claude()
    version = ""
    if bin_path:
        try:
            out = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=30)
            version = (out.stdout or out.stderr).strip()[:120]
        except Exception:
            version = "?"
    settings_file = detect._home() / ".claude" / "settings.json"
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            settings = {"_error": "unparseable"}
    aliases = {
        "ANTHROPIC_DEFAULT_SONNET_MODEL": os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", ""),
    }
    skills_dir = detect._home() / ".claude" / "skills"
    skill_count = len([p for p in skills_dir.glob("*/SKILL.md")]) if skills_dir.exists() else 0
    return {
        "ok": True,
        "binary": bin_path or "",
        "version": version,
        "skills_count": skill_count,
        "settings": settings,
        "settings_path": str(settings_file),
        "aliases": aliases,
    }


# ── HTTP handler ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        token = _auth_token()
        given = self.headers.get("X-Atropos-Token", "")
        if not given:
            q = parse_qs(urlparse(self.path).query)
            given = (q.get("token") or [""])[0]
        return given == token

    def _send(self, status, body_bytes, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_bytes)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _route_get(self, path, q):
        """Return data for a GET path, or None if unhandled."""
        if path in ("/", "/index.html"):
            f = DASHBOARD_DIR / "index.html"
            if f.exists():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"dashboard/index.html missing")
            return True
        if not path.startswith("/api/"):
            return None
        api = {
            "/api/status": api_status,
            "/api/version": api_version,
            "/api/doctor": lambda: api_doctor(),
            "/api/patches": api_patches,
            "/api/route": lambda: api_route(),
            "/api/guest": api_guest,
            "/api/sessions": api_sessions,
            "/api/models": api_models,
            "/api/cron": api_cron,
            "/api/skills": api_skills,
            "/api/plugins": api_plugins,
            "/api/channels": api_channels,
            "/api/hermes-config": api_hermes_config,
            "/api/analytics": api_analytics,
            "/api/files": api_files,
            "/api/backup": lambda: api_backup(),
            "/api/history": lambda: api_history(),
            "/api/claude": api_claude,
            "/api/claude/settings": lambda: api_claude_settings(),
            "/api/config": lambda: api_config_get(),
        }
        if path in api:
            return api[path]()
        if path == "/api/logs":
            tail = int((q.get("tail") or ["80"])[0])
            return api_logs(tail=tail)
        if path == "/api/route/test":
            return api_route_test()
        if path.startswith("/api/config/"):
            raw_key = path.split("/api/config/", 1)[1]
            return api_config_get(raw_key)
        if path == "/api/guest/persona":
            return api_guest_persona(method="GET")
        return {"ok": False, "error": f"unknown api: {path}"}

    def _route_post(self, path, payload):
        if path == "/api/doctor/fix":
            return api_doctor(fix=True)
        if path == "/api/route/set":
            return api_route("set", payload.get("name"))
        if path == "/api/route/test":
            return api_route_test(payload.get("name"))
        if path == "/api/update/apply":
            return api_update(check=False)
        if path == "/api/config/set":
            return api_config_set(payload.get("key"), payload.get("value"))
        if path == "/api/guest/toggle":
            return api_guest_toggle()
        if path == "/api/guest/persona":
            return api_guest_persona(method="POST", content=payload.get("content"))
        if path == "/api/backup/now":
            return api_backup(trigger=True)
        if path == "/api/patches/apply":
            return api_patches_apply()
        if path == "/api/route/apply":
            return api_route_apply()
        if path == "/api/claude/settings":
            return api_claude_settings(payload.get("content"))
        if path == "/api/hermes-config":
            # optional save: {"content": "..."} writes the file back
            content = payload.get("content")
            if content is None:
                return {"ok": False, "error": "content required"}
            p = detect.hermes_home() / "config.yaml"
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                history_log("hermes-config", "saved")
                return {"ok": True, "saved": str(p)}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"unknown api: {path}"}

    def do_GET(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path in ("/", "/index.html"):
            self._route_get(path, q)
            return
        if path.startswith("/api/"):
            if not self._auth():
                self._send(401, b'{"error":"unauthorized"}')
                return
            data = self._route_get(path, q)
            if data is not None:
                body, ctype, status = _body(200, data)
                self._send(status, body, ctype)
                return
            self._send(404, b'{"error":"not found"}')
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            self._send(401, b'{"error":"unauthorized"}')
            return
        payload = self._read_json()
        data = self._route_post(path, payload)
        body, ctype, status = _body(200, data)
        self._send(status, body, ctype)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Atropos-Token")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


def serve(host="127.0.0.1", port=8787):
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Atropos dashboard on http://{host}:{port}")
    print(f"Token: {_auth_token()}")
    history_log("dashboard", "started")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cfg = config.load()
    d = cfg.get("dashboard", {})
    serve(d.get("host", "127.0.0.1"), int(d.get("port", 8787)))
