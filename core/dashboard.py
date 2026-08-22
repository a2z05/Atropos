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
import queue
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

from . import config, detect, doctor, guest, logs, patches, router, settings, skills, update

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


def _error(code: str, message: str, details=None, status: int = 400) -> dict:
    """Structured error envelope (benchmark area 28 adoption).

    Stable machine-readable ``code`` + human ``message`` + optional
    ``details``. Backward compatible with the flat
    ``{"ok": False, "error": "..."}`` shape (``error`` stays present).
    """
    out = {"ok": False, "error": message}
    wrapped = {"code": code, "message": message}
    if details is not None:
        wrapped["details"] = details
    out["error_obj"] = wrapped
    out["_status"] = status
    return out


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── process uptime ───────────────────────────────────────────────────────
_PROC_START = time.monotonic()


def _uptime():
    return round(time.monotonic() - _PROC_START)


# ── history (dashboard action log) ───────────────────────────────────────
def history_log(action: str, detail=""):
    """Append one action to ~/.atropos/history.jsonl.

    Secret values are masked (settings keys flagged secret are never
    written — the token stays out of the audit trail).
    """
    try:
        p = detect.atropos_home() / "history.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        detail = _mask_history_detail(action, detail)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _ts(), "action": action, "detail": detail}) + "\n")
    except Exception:
        pass


def _mask_history_detail(action: str, detail: str) -> str:
    """Replace secret values inside a history detail string."""
    try:
        for key in ("alerts.token", "dashboard.password"):
            if key in detail:
                spec_key = key
                node = None
                # detail format is "key=value" — mask the value side
                marker = f"{key}="
                if marker in detail:
                    val = detail.split(marker, 1)[1].split(" ", 1)[0].rstrip(",")
                    detail = detail.replace(marker + val, marker + settings.SECRET_MASK)
    except Exception:
        pass
    return detail


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


_BUILD = _version()


def api_version():
    return {
        "ok": True,
        "version": _version(),
        "build": _BUILD,
        "sha": _git_sha(),
    }


def api_health():
    """v18 B.4 — GET /health: status, version, cloud, uptime (unauthenticated)."""
    from . import detect
    cloud = detect.detect_cloud()
    return {
        "status": "ok",
        "version": _version(),
        "cloud": cloud,
        "uptime": f"{_uptime() // 3600}h {(_uptime() % 3600) // 60}m",
        "railway": cloud == "railway",
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


def _sql_scalar(query: str, default=0, args=None):
    db = _find_state_db()
    if not db.exists():
        return default
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cur = con.cursor()
            cur.execute(query, args or ())
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


def _tables_in(db: Path):
    """Table names in a sqlite db, or []."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            return [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        finally:
            con.close()
    except Exception:
        return []


# ── update state (dashboard-native, no cron) ────────────────────────────
def _update_state_file() -> Path:
    return detect.atropos_home() / "update_state.json"


def _update_state():
    try:
        p = _update_state_file()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_update_state(state: dict):
    try:
        p = _update_state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


# ── router latency history (for sparklines, capped at 200) ──────────────
def _router_history_file() -> Path:
    return detect.atropos_home() / "router_history.json"


def _router_history():
    try:
        p = _router_history_file()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_router_history(data: dict):
    try:
        p = _router_history_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _append_router_sample(name: str, ok: bool, latency_ms):
    data = _router_history()
    samples = data.get(name, [])
    samples.append({"ts": _ts(), "ok": bool(ok), "latency_ms": latency_ms})
    if len(samples) > 200:
        samples = samples[-200:]
    data[name] = samples
    _save_router_history(data)


# ── changelog / version ──────────────────────────────────────────────────
def api_changelog():
    p = REPO_DIR / "docs" / "CHANGELOG.md"
    if p.exists():
        try:
            content = p.read_text(encoding="utf-8")
            return {"ok": True, "exists": True, "content": content[-12000:]}
        except Exception as e:
            return {"ok": True, "exists": True, "error": str(e), "content": ""}
    return {"ok": True, "exists": False, "content": ""}


def api_session_trace(session_id):
    """Messages for one session from state.db (last 20, truncated 500 chars)."""
    db = _find_state_db()
    if not db.exists():
        return {"ok": False, "error": "state.db not found"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            tables = _tables_in(db)
            msg_tbl = next((t for t in tables if "message" in t.lower()), None)
            if not msg_tbl:
                return {"ok": True, "messages": [], "table": None, "note": "no messages table"}
            cols = [d[1] for d in con.execute(f"PRAGMA table_info({msg_tbl})")]
            # find the column that links messages back to a session
            link_col = next((c for c in ("session_id", "session", "conversation_id",
                                         "convo_id", "chat_id", "channel", "user_id") if c in cols), None)
            role_col = next((c for c in cols if "role" in c.lower()), None)
            content_col = next((c for c in cols if c.lower() in
                                ("content", "text", "message", "body", "text_content")), None)
            ts_col = next((c for c in cols if any(
                t in c.lower() for t in ("ts", "time", "date", "created"))), None)
            where = f" WHERE CAST({link_col} AS TEXT)=?" if link_col else ""
            cur = con.cursor()
            cur.execute(f"SELECT * FROM {msg_tbl}{where} ORDER BY rowid DESC LIMIT 20",
                        (str(session_id),) if link_col else ())
            rows = cur.fetchall()
            names = [d[0] for d in cur.description]
            out = []
            for r in rows:
                rec = dict(zip(names, r))
                content = ""
                if content_col and rec.get(content_col) is not None:
                    content = str(rec[content_col])[:500]
                out.append({
                    "role": str(rec.get(role_col, "?"))[:20] if role_col else "?",
                    "ts": str(rec.get(ts_col, ""))[:30] if ts_col else "",
                    "length": len(content),
                    "content": content,
                })
            out.reverse()  # chronological order
            return {"ok": True, "messages": out, "table": msg_tbl,
                    "link_col": link_col, "total": len(out)}
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_analytics_cost():
    """Estimate spend from state.db if token counts are present."""
    db = _find_state_db()
    if not db.exists():
        return {"ok": True, "available": False, "reason": "state.db not found"}
    tables = _tables_in(db)
    msg_tbl = next((t for t in tables if "message" in t.lower()), None)
    if not msg_tbl:
        return {"ok": True, "available": False, "reason": "no messages table"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cols = [d[1] for d in con.execute(f"PRAGMA table_info({msg_tbl})")]
            tok_col = next((c for c in cols if "token" in c.lower()), None)
            if not tok_col:
                return {"ok": True, "available": False,
                        "reason": "no token column in messages — enable usage tracking"}
            total = con.execute(f"SELECT COALESCE(SUM({tok_col}),0) FROM {msg_tbl}").fetchone()[0]
            provider_col = next((c for c in cols if any(
                k in c.lower() for k in ("router", "provider", "channel", "platform"))), None)
            by_router = []
            if provider_col:
                rows = con.execute(
                    f"SELECT {provider_col}, COUNT(*), COALESCE(SUM({tok_col}),0) "
                    f"FROM {msg_tbl} GROUP BY {provider_col}").fetchall()
                by_router = [{"name": str(r[0]), "count": r[1], "tokens": int(r[2])} for r in rows]
            return {"ok": True, "available": True, "total_tokens": int(total),
                    "by_router": by_router, "column": tok_col}
        finally:
            con.close()
    except Exception as e:
        return {"ok": True, "available": False, "reason": str(e)}


def api_router_history():
    return {"ok": True, "history": _router_history()}


# ── backup schedule (dashboard-native) ──────────────────────────────────
def api_backup_config():
    return {"ok": True, "period": settings.get("backup.period", "off")}


def api_backup_config_set(payload):
    period = (payload or {}).get("period", "off")
    try:
        settings.set("backup.period", period)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    history_log("backup", f"period={period}")
    return {"ok": True, "period": period}


# ── dashboard password gate ──────────────────────────────────────────────
def api_auth_check(payload):
    """Optional password gate. If dashboard.password is set in config, the
    dashboard frontend shows a password field before accepting a token."""
    cfg = config.load()
    pw = (cfg.get("dashboard", {}) or {}).get("password", "")
    if not pw:
        return {"ok": True, "required": False}
    given = (payload or {}).get("password", "")
    return {"ok": given == pw, "required": True}


# ── claude doctor runner ────────────────────────────────────────────────
def api_claude_doctor():
    bin_path = detect._find_claude()
    if not bin_path:
        return {"ok": False, "error": "claude binary not found"}
    try:
        out = subprocess.run([bin_path, "doctor"], capture_output=True, text=True, timeout=90)
        combined = (out.stdout or "") + (out.stderr or "")
        return {"ok": True, "exit": out.returncode, "output": combined[-4000:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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


# ── v19 M5: Single Session Engine API ────────────────────────────────────
def api_session_engine_config(payload=None):
    """Session Engine config: mode cards + per-surface modes + tunables."""
    from . import session_engine as _se, settings as _settings
    cards = _se.MODE_CARDS
    overrides = {}
    for surface in ("telegram", "dashboard", "cli", "agents"):
        overrides[surface] = _settings.get(f"session_engine.surfaces.{surface}", None)
    return {
        "ok": True,
        "mode": _settings.get("session_engine.mode", "unified"),
        "cards": cards,
        "surfaces": {s: _se.surface_mode(s) for s in ("telegram", "dashboard", "cli", "agents")},
        "overrides": overrides,
        "tunables": {
            "classifier": _settings.get("session_engine.classifier", "cheap"),
            "affinity_bias": _settings.get("session_engine.affinity_bias", 0.8),
            "confidence_threshold": _settings.get("session_engine.confidence_threshold", 0.6),
            "mirror_on_deep_switch": _settings.get("session_engine.mirror_on_deep_switch", True),
            "new_topic_min_messages": _settings.get("session_engine.new_topic_min_messages", 3),
            "session_titles": _settings.get("session_engine.session_titles", "auto"),
            "max_sessions": _settings.get("session_engine.max_sessions", 50),
            "hybrid_confidence": _settings.get("session_engine.hybrid_confidence", 0.9),
            "hybrid_min_depth": _settings.get("session_engine.hybrid_min_depth", 25),
            "hybrid_max_split_sessions": _settings.get("session_engine.hybrid_max_split_sessions", 6),
        },
    }


def api_session_engine_config_set(payload):
    """POST /api/session_engine/config {mode?, surfaces?, tunables?}."""
    from . import session_engine as _se, settings as _settings
    errors = []
    mode = (payload or {}).get("mode")
    if mode:
        try:
            _settings.set("session_engine.mode", mode)
        except ValueError as e:
            errors.append(str(e))
    surfaces = (payload or {}).get("surfaces") or {}
    for surface, m in surfaces.items():
        try:
            _settings.set(f"session_engine.surfaces.{surface}", m)
        except ValueError as e:
            errors.append(f"{surface}: {e}")
    tunables = (payload or {}).get("tunables") or {}
    for key, val in tunables.items():
        if key in ("classifier", "affinity_bias", "confidence_threshold",
                   "mirror_on_deep_switch", "new_topic_min_messages",
                   "session_titles", "max_sessions", "hybrid_confidence",
                   "hybrid_min_depth", "hybrid_max_split_sessions"):
            try:
                _settings.set(f"session_engine.{key}", val)
            except ValueError as e:
                errors.append(f"{key}: {e}")
    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "config": api_session_engine_config()}


def api_session_engine_stats():
    """POST /api/session_engine/stats — engine counters + mode state."""
    from . import session_engine as _se
    st = _se.stats()
    st["ok"] = True
    return st


def api_sessions_route(payload):
    """POST /api/sessions/route {session_id, surface} — manual route."""
    from . import session_engine as _se
    surface = (payload or {}).get("surface", "cli")
    sid = (payload or {}).get("session_id", "")
    if not sid:
        return {"ok": False, "error": "session_id required"}
    r = _se.switch_session(surface, sid)
    r["surface"] = surface
    return r


def api_sessions_merge(payload):
    """POST /api/sessions/merge {a, b, surface} — merge b into a."""
    from . import session_engine as _se
    surface = (payload or {}).get("surface", "cli")
    a = (payload or {}).get("a", "")
    b = (payload or {}).get("b", "")
    return _se.merge_sessions(surface, a, b)


def api_sessions_list_detailed(payload=None):
    """GET/POST /api/sessions/detailed — engine-decorated sessions."""
    from . import session_engine as _se
    return {"ok": True, "sessions": _se.sessions_detailed()}


def api_session_engine_explain(payload):
    """POST /api/session_engine/explain {message} — decision trail."""
    from . import session_engine as _se
    msg = (payload or {}).get("message", "")
    if not msg:
        return {"ok": False, "error": "message required"}
    surface = (payload or {}).get("surface", "cli")
    return {"ok": True, "text": _se.explain(msg, surface)}


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


def _cron_state_file():
    """JSON state file holding last_run/next_run for cron jobs."""
    return detect.atropos_home() / "cron_state.json"


def _cron_state():
    try:
        p = _cron_state_file()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cron_state(state):
    try:
        p = _cron_state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def api_cron():
    """Cron jobs from $HERMES_HOME/cron/*.yaml."""
    cron_dir = detect.hermes_home() / "cron"
    state = _cron_state()
    jobs = []
    for f in _yaml_files(cron_dir):
        try:
            data = config.parse_yaml(f.read_text(encoding="utf-8"))
            st = state.get(f.stem, {})
            jobs.append({
                "job_id": f.stem,
                "name": data.get("name", f.stem),
                "file": f.name,
                "schedule": data.get("schedule", data.get("cron", "")),
                "command": data.get("command", data.get("task", "")),
                "enabled": data.get("enabled", True),
                "last_run": st.get("last_run"),
                "next_run": st.get("next_run"),
            })
        except Exception as e:
            jobs.append({
                "job_id": f.stem, "name": f.stem, "file": f.name,
                "schedule": "", "command": "", "enabled": True,
                "error": str(e),
            })
    return {"ok": True, "dir": str(cron_dir), "exists": cron_dir.exists(), "jobs": jobs}


def api_cron_toggle(job_id: str, resume: bool):
    """Pause/resume a cron job by editing its `enabled:` line in place.

    Edits the exact line (preserving comments/order) rather than re-encoding
    the whole file, so YAML comment headers survive.
    """
    cron_dir = detect.hermes_home() / "cron"
    targets = list(_yaml_files(cron_dir))
    f = None
    for cand in targets:
        if cand.stem == job_id:
            f = cand
            break
    if not f:
        return {"ok": False, "error": f"cron job not found: {job_id}"}
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}
    new_val = "true" if resume else "false"
    idx = None
    for i, ln in enumerate(lines):
        s = ln.rstrip()
        # match a top-level `enabled:` key (no leading space → top level)
        if s.startswith("enabled:") and not ln.startswith(" "):
            idx = i
            break
    if idx is not None:
        before = lines[idx].split("enabled:", 1)[0]  # preserves any inline comment
        lines[idx] = before + "enabled: " + new_val
    else:
        lines.append("enabled: " + new_val)
    try:
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}"}
    action = "resume" if resume else "pause"
    history_log("cron", f"{action} {job_id}")
    return {"ok": True, "job_id": job_id, "enabled": resume, "action": action}


def api_skills():
    """Skills from $HERMES_HOME/skills/*/SKILL.md (and ~/.claude/skills).

    Includes universal skills from ~/.atropos/skills with category + harness routing
    when the skills module is available.
    """
    dirs = [detect.hermes_home() / "skills", detect._home() / ".claude" / "skills"]
    skills = []
    seen = set()
    for d in dirs:
        if not d.exists():
            continue
        for sk in sorted(d.iterdir()):
            if sk.name in seen:
                continue
            seen.add(sk.name)
            md = sk / "SKILL.md"
            if md.exists():
                try:
                    head = md.read_text(encoding="utf-8", errors="replace")[:240]
                except Exception:
                    head = ""
                meta = {}
                if head.startswith("---"):
                    end = head.find("---", 3)
                    if end > 0:
                        for line in head[3:end].splitlines():
                            if ":" in line:
                                k, _, v = line.partition(":")
                                meta[k.strip()] = v.strip()
                skills.append({
                    "name": sk.name,
                    "path": str(md),
                    "source": str(d),
                    "head": head,
                    "category": meta.get("category", ""),
                    "description": meta.get("description", ""),
                    "harness": meta.get("harness", ""),
                })
    # Merge in universal skills (atropos/skills) with routing
    try:
        for s in skills.list_skills():
            if s["name"] not in seen:
                skills.append({
                    "name": s["name"],
                    "path": s["path"],
                    "source": "atropos/skills",
                    "head": s.get("description", ""),
                    "category": s.get("category", ""),
                    "description": s.get("description", ""),
                    "harness": s.get("harness", "hermes"),
                })
                seen.add(s["name"])
    except Exception:
        pass
    # enrich with usage lifecycle telemetry (v18 F) + lint status (v18 I)
    try:
        from . import autoskill
        stats = autoskill.usage_stats()
        for s in skills:
            rec = stats.get(s["name"])
            if rec:
                s["usage"] = {k: rec.get(k) for k in ("view", "run", "last_used")}
                s["lifecycle"] = rec.get("lifecycle", "active")
    except Exception:
        pass
    try:
        from . import skills as _sk
        lint = _sk.skill_lint()
        lint_by = {it["skill"]: it["errors"] for it in lint.get("issues", [])}
        sdir = _sk.skills_dir()
        for s in skills:
            # only universal-store entries get lint status (hermes/claude
            # stores are scanned read-only
            rel = None
            try:
                rel = Path(s["path"]).resolve().relative_to(sdir.resolve())
            except Exception:
                rel = None
            if rel is not None:
                s["lint"] = lint_by.get(s["name"]) or []
    except Exception:
        pass
    return {"ok": True, "skills": skills}


def api_autoskill_usage():
    """Usage lifecycle telemetry for the Skills panel (v18 F)."""
    from . import autoskill
    return {"ok": True, "usage": autoskill.usage_stats(),
            "curator": autoskill.curator_status()}


def api_curator_run(consolidate: bool = False):
    """Run the skill curator on demand."""
    from . import autoskill
    report = autoskill.curator_run(consolidate=consolidate)
    return {"ok": True, **report}


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


def api_hermes_skills():
    """List hermes skills dir contents (dirs + SKILL.md detection)."""
    hermes_dir = detect.hermes_home() / "skills"
    skills = []
    if hermes_dir.exists():
        for p in sorted(hermes_dir.iterdir()):
            if not p.is_dir():
                continue
            md = p / "SKILL.md"
            head = ""
            if md.exists():
                try:
                    head = md.read_text(encoding="utf-8", errors="replace")[:240]
                except Exception:
                    head = ""
            skills.append({
                "name": p.name,
                "path": str(md),
                "symlink": p.is_symlink(),
                "head": head,
            })
    return {"ok": True, "dir": str(hermes_dir), "exists": hermes_dir.exists(), "skills": skills}


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
    # messages today — crude but env-agnostic: any timestamp col whose string
    # starts with today's YYYY-MM-DD. Falls back to 'n/a' when no column works.
    msgs_today = "n/a"
    avg = "n/a"
    try:
        if "messages" in tables:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                cols = [d[1] for d in con.execute("PRAGMA table_info(messages)")]
            finally:
                con.close()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            candidates = [c for c in cols if any(
                t in c.lower() for t in ("ts", "time", "date", "created"))]
            for c in candidates:
                try:
                    v = _sql_scalar(
                        f"SELECT COUNT(*) FROM messages WHERE substr({c},1,10)=?",
                        default=None, args=(today,))
                    if v is not None:
                        msgs_today = v
                        break
                except Exception:
                    continue
            if isinstance(msgs, int) and isinstance(sesses, int) and sesses:
                avg = round(msgs / sesses, 1)
    except Exception:
        pass
    return {
        "ok": True,
        "messages": msgs,
        "sessions": sesses,
        "messages_today": msgs_today,
        "avg_msgs_per_session": avg,
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
        "config": settings.mask_secrets(config.load()),
        "uptime": _uptime(),
        "ts": _ts(),
    }


def api_doctor(fix=False):
    if fix:
        history_log("doctor", "fix")
    return {"ok": True, "checks": doctor.doctor(fix=fix)}


def api_patches():
    """Integration status — the code-defined customizations, no YAML hacks."""
    hacks = {h["id"]: h for h in patches.load_hacks()}
    results = patches.verify()
    for r in results:
        h = hacks.get(r["id"], {})
        r["file"] = "core/patches.py"  # source of truth is code
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
        result = update.update_check(repo)
        result["last_check"] = _ts()
        _save_update_state({
            "last_check": result["last_check"],
            "up_to_date": result.get("up_to_date", False),
            "behind": result.get("behind"),
            "head": result.get("head"),
            "remote": result.get("remote"),
        })
        return result
    history_log("update", "apply")
    _activity_log("update", "apply")
    # changelog auto-bump lives in the Atropos repo (this repo), not the
    # hermes-agent repo that apply_update resets.
    changelog_result = None
    if repo:  # hermes-agent repo — bump only on successful apply below
        pass
    result = update.apply_update(repo)
    if result.get("ok") and settings.get("update.changelog_bump", True):
        try:
            from .update import bump_changelog as _bump
            changelog_result = _bump(REPO_DIR / "docs" / "CHANGELOG.md",
                                     version=_version(), source=result.get("head", ""))
            result["changelog"] = changelog_result
        except Exception as e:
            result["changelog"] = {"ok": False, "error": str(e)}
    result["last_check"] = _ts()
    _save_update_state({
        "last_check": result["last_check"],
        "up_to_date": result.get("up_to_date", False),
        "behind": result.get("behind"),
        "head": result.get("head"),
        "remote": result.get("remote"),
    })
    return result


def api_update_state():
    """Last update check/apply state + changelog snippet."""
    state = _update_state()
    return {"ok": True, **state}


def api_route(action=None, name=None):
    if action == "set" and name:
        try:
            r = router.set_active(name)
            router.apply_all()
            history_log("route", name)
            return {"ok": True, "router": r, "available": _router_available()}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "router": router.get(), "available": _router_available()}


def _router_available():
    """Rich listing of configured routers: [{name, model, base_url, description, active}]."""
    active = router.get().get("active", "")
    out = []
    for name, info in router.ROUTERS.items():
        out.append({
            "name": name,
            "model": info["model"],
            "base_url": info["base_url"] or "(env OPENAI_BASE_URL)",
            "description": info["description"],
            "api_key_env": info["api_key_env"],
            "active": name == active,
        })
    return out


def api_route_test(name=None):
    """Live-ping a router (defaults to all available). Records samples for sparklines."""
    names = [name] if name else router.available()
    results = {}
    for n in names:
        r = router.ping(n)
        results[n] = r
        _append_router_sample(n, r.get("ok", False), r.get("latency_ms"))
    return {"ok": True, "results": results}


def api_route_apply():
    """Push the active router to Hermes .env + Claude settings.json."""
    results = router.apply_all()
    history_log("route", "applied to hermes+claude")
    return {"ok": True, "results": results}


def api_config_get(key=None):
    """Read a config key (schema-validated; group reads are masked)."""
    if key == "version":
        return {"ok": True, "value": _version()}
    if key is None:
        return {"ok": True, "config": settings.mask_secrets(config.load()), "version": _version()}
    if key not in settings.schema():
        return {"ok": False, "error": f"unknown config key: {key}"}
    val = config.get(key)
    if settings.is_secret(key) and val:
        val = settings.SECRET_MASK
    return {"ok": True, "value": val}


def api_config_set(key=None, value=None):
    """Set a config key. Validated through the settings schema.

    Unknown keys are rejected; wrong types raise a clear error. Back-compat
    wrapper over settings.set so the old panel keeps working.
    """
    if not key:
        return {"ok": False, "error": "key required"}
    try:
        settings.set(key, value)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if settings.is_secret(key):
        logged_value = settings.SECRET_MASK
    else:
        logged_value = json.dumps(value, ensure_ascii=False)
    history_log("config", f"{key}={logged_value}")
    final = settings.SECRET_MASK if settings.is_secret(key) else config.get(key)
    return {"ok": True, "key": key, "value": final}


def api_guest():
    st = guest.status()
    try:
        st["sealed"] = guest.sealed_owner_view()  # counts only (v18 K)
    except Exception:
        st["sealed"] = []
    return {"ok": True, **st}


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


def api_effort():
    """Current per-harness effort tiers from settings."""
    effort = {
        "hermes": settings.get("effort.hermes", "medium"),
        "claude": settings.get("effort.claude", "medium"),
        "atropos": settings.get("effort.atropos", "medium"),
    }
    tiers = {
        "minimal": "Fastest responses, least reasoning tokens, short answers",
        "low": "Low reasoning, quick answers, minimal tool use",
        "medium": "Balanced reasoning, standard tool use, normal responses",
        "high": "Deep reasoning, long context, exhaustive research",
        "xhigh": "Maximum reasoning, very deep analysis, full agentic mode",
        "ultracode": "Ultra reasoning, every tool, multi-delegate, deep reasoning",
        "tryhard": "ABSOLUTE MAXIMUM PERFORMANCE — every optimization, zero compromise",
    }
    return {
        "ok": True,
        "current": effort,
        "tiers": tiers,
        "available": list(tiers.keys()),
    }


def api_effort_set(payload):
    """Set effort tier for one or more harnesses (via settings schema)."""
    tier = (payload or {}).get("tier", "")
    targets = (payload or {}).get("targets", [])
    available = settings.EFFORT_TIERS
    if tier not in available:
        return {"ok": False, "error": f"unknown tier: {tier}. Available: {', '.join(available)}"}
    valid_targets = ["hermes", "claude", "atropos"]
    if not targets:
        targets = valid_targets
    targets = [t for t in targets if t in valid_targets]
    if not targets:
        return {"ok": False, "error": "no valid targets"}
    try:
        for t in targets:
            settings.set(f"effort.{t}", tier)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    current = {t: settings.get(f"effort.{t}") for t in ("hermes", "claude", "atropos")}
    history_log("effort", f"set {tier} for {', '.join(targets)}")
    return {"ok": True, "current": current, "changed": targets}


def api_backup_list():
    """List all backups."""
    try:
        from . import backup as backup_mod
        backups = backup_mod.list_backups()
        return {"ok": True, "backups": backups, "dir": str(backup_mod.backup_dir())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_backup_create():
    """Create a new backup."""
    try:
        from . import backup as backup_mod
        result = backup_mod.create()
        history_log("backup", f"created {result.get('path', '?')} size={result.get('size_mb', '?')}MB")
        _activity_log("backup", result.get("path", ""))
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_router_models():
    """List available models from the active router's /v1/models endpoint."""
    import urllib.request
    import urllib.error
    cfg = config.load()
    r = cfg.get("router", {})
    base_url = r.get("base_url", "") or os.environ.get("OPENAI_BASE_URL", "")
    # Build models endpoint URL
    if base_url:
        endpoint = base_url.rstrip("/") + "/models"
    else:
        endpoint = "https://api.openai.com/v1/models"
    api_key = os.environ.get(r.get("api_key_env", ""), "")
    headers = {"Content-Type": "application/json"}
    if api_key and r.get("api_key_env") != "OLLAMA_HOST":
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(endpoint, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            models = [m.get("id", "") for m in data.get("data", [])]
            return {"ok": True, "models": models, "endpoint": endpoint}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}", "endpoint": endpoint}
    except Exception as e:
        return {"ok": False, "error": str(e), "endpoint": endpoint}


def api_update_version():
    """Current version + git info."""
    return {
        "ok": True,
        "version": _version(),
        "sha": _git_sha(),
    }


def api_self_heal():
    """Run the full self-healing pipeline: doctor --fix → patch verify/apply → watch.
    Returns each stage's result. Safe: patches are re-applied only if verify fails."""
    from . import doctor as _doc
    from . import patches as _pat
    from .watch import run_watch
    stages = []

    # 1. doctor + fix
    try:
        res = _doc.doctor(fix=True)
        stages.append({
            "stage": "doctor",
            "ok": all(r["ok"] for r in res),
            "detail": [{"name": r["name"], "ok": r["ok"], "msg": r["msg"]} for r in res],
        })
    except Exception as e:
        stages.append({"stage": "doctor", "ok": False, "error": str(e)})

    # 2. patch verify → apply if needed
    try:
        verified = _pat.verify()
        need_apply = [r for r in verified if not r["applied"]]
        if need_apply:
            applied, skipped, errors = _pat.apply_hacks()
            stages.append({
                "stage": "patches",
                "ok": len(errors) == 0,
                "repaired": [a for a in applied],
                "errors": [str(e) for e in errors],
            })
        else:
            stages.append({"stage": "patches", "ok": True, "repaired": [], "msg": "all 12 applied"})
    except Exception as e:
        stages.append({"stage": "patches", "ok": False, "error": str(e)})

    # 3. watch (disk/log/health)
    try:
        w = run_watch()
        stages.append({
            "stage": "watch",
            "ok": w.get("ok", True),
            "alerts": w.get("alerts", []),
        })
    except Exception as e:
        stages.append({"stage": "watch", "ok": False, "error": str(e)})

    return {"ok": all(s["ok"] for s in stages), "stages": stages}


def api_alert_test():
    from .alerts import send_alert
    ok = send_alert("Test alert from Atropos dashboard ✅", force=True)
    return {"ok": ok, "msg": "test alert sent" if ok else "alert failed/config missing"}


def api_alert_check():
    import shutil
    from .alerts import check_and_alert
    usage = shutil.disk_usage(str(detect.hermes_home()))
    pct = usage.used / usage.total * 100
    sent = check_and_alert(disk_pct=pct)
    return {"ok": True, "disk_pct": round(pct, 1), "alerts_sent": sent}


def api_jailbreak_status():
    from .jailbreak import scan
    return {"ok": True, "restrictions": scan()}


def api_jailbreak_apply():
    from .jailbreak import apply
    # read `id` from POST? This GET-lambda version applies only if id provided
    return {"ok": False, "error": "use /api/jailbreak/apply-all or POST with id"}


def api_jailbreak_apply_all():
    from .jailbreak import apply_all
    return {"ok": True, "results": apply_all()}


def api_jailbreak_revert():
    from .jailbreak import revert, scan as _scan
    # no id → list available
    return {"ok": True, "ids": [r["id"] for r in _scan()]}


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


# ── v1.2.0: settings / extensions / market / console / failover ─────────
def api_settings():
    """Full settings surface: schema + current values (secrets masked)."""
    groups_out = {}
    for key, spec in settings.schema().items():
        gname = spec.get("group", "core")
        groups_out.setdefault(gname, []).append({
            "key": key,
            "type": spec["type"],
            "description": spec.get("description", ""),
            "choices": spec.get("choices") or (spec.get("item_choices") if spec["type"] == "list" else None),
            "secret": bool(spec.get("secret")),
            "readonly": bool(spec.get("readonly")),
            "value": settings.SECRET_MASK if (spec.get("secret") and settings.get(key))
                     else settings.get(key),
        })
    return {
        "ok": True,
        "build": _BUILD,
        "groups": groups_out,
        "defaults": settings.mask_secrets({k: v.get("default") for k, v in settings.schema().items()}),
        "theme": settings.get("dashboard.theme", "auto"),
        "global_theme": settings.get("theme", "dark"),
        "lang": settings.get("dashboard.lang", "en"),
        "cli_lang": settings.get("cli.lang", "en"),
        "beta_badge": settings.get("beta_badge", True),
        "accent": settings.get("dashboard.accent", "indigo"),
        "particles": settings.get("dashboard.particles", True),
        "live": settings.get("dashboard.live", True),
        "refresh_ms": settings.get("dashboard.refresh_ms", 10000),
        "cli_default_action": settings.get("cli.default_action", "cli"),
        "update_auto": settings.get("update.auto", "off"),
        "update_auto_ai": settings.get("update.auto_ai", False),
        "update_ai": {
            "mode": settings.get("update-ai.mode", "manual"),
            "model": settings.get("update-ai.model", "deepmo"),
            "effort": settings.get("update-ai.effort", "medium"),
        },
    }


def api_settings_set(payload):
    """POST /api/settings/set {key, value} — validated through settings.py."""
    key = (payload or {}).get("key", "")
    value = (payload or {}).get("value")
    if not key:
        return {"ok": False, "error": "key required"}
    try:
        settings.set(key, value)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    final = settings.SECRET_MASK if settings.is_secret(key) else settings.get(key)
    if settings.is_secret(key):
        history_log("settings", f"{key}={settings.SECRET_MASK}")
    else:
        history_log("settings", f"{key}={json.dumps(final, ensure_ascii=False)}")
    return {"ok": True, "key": key, "value": final, "settings": api_settings()}


def api_settings_export():
    """Full YAML export (secrets masked unless ?secrets=1)."""
    return {"ok": True, "yaml": settings.export_yaml(include_secrets=False)}


def api_settings_import(payload):
    """Import a YAML settings blob (validated)."""
    yaml_text = (payload or {}).get("yaml", "")
    if not yaml_text:
        return {"ok": False, "error": "yaml required"}
    try:
        settings.import_yaml(yaml_text)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    history_log("settings", "imported settings")
    return {"ok": True, "settings": api_settings()}


def api_extensions(kind="all"):
    """Unified extension listing (skills + plugins)."""
    try:
        from . import extensions
        items = extensions.list_extensions(kind)
        return {"ok": True, "items": items, "count": len(items),
                "trash": extensions.list_trash()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_extension_action(payload):
    """POST /api/extensions/action {action, name, kind, source}."""
    from . import extensions
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    kind = (payload or {}).get("kind", "skill")
    source = (payload or {}).get("source", "hermes")
    if not extensions.valid_name(name):
        return {"ok": False, "error": f"invalid name: {name!r}"}
    try:
        if action == "enable":
            result = extensions.enable(name, kind, source)
        elif action == "disable":
            result = extensions.disable(name, kind, source)
        elif action == "remove":
            result = extensions.remove(name, kind, source)
        elif action == "restore":
            result = extensions.restore_from_trash(name, kind, source)
        elif action == "empty-trash":
            result = {"ok": True, "removed": extensions.empty_trash()}
        else:
            return {"ok": False, "error": f"unknown action: {action}"}
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    history_log("extensions", f"{action} {name} ({kind}/{source})")
    if result.get("ok"):
        _notify("extensions", {"action": action, "name": name, "kind": kind})
    return {"ok": True, **result}


def api_marketplace():
    """Marketplace catalog with install state."""
    from . import marketplace
    return marketplace.catalog()


def api_marketplace_install(payload):
    """Install a catalog item."""
    from . import marketplace
    source_id = (payload or {}).get("source", "")
    item = (payload or {}).get("item", "")
    result = marketplace.install(source_id, item)
    if result.get("ok"):
        history_log("marketplace", f"installed {item} from {source_id}")
        _notify("marketplace", {"action": "install", "item": item, "source": source_id})
    return result


def api_marketplace_uninstall(payload):
    """Uninstall a marketplace item (→ trash)."""
    from . import marketplace
    item = (payload or {}).get("item", "")
    kind = (payload or {}).get("kind", "skill")
    target = (payload or {}).get("target", "hermes")
    result = marketplace.uninstall(item, kind, target)
    if result.get("ok"):
        history_log("marketplace", f"uninstalled {item}")
    return result


def api_marketplace_source_add(payload):
    """Add a custom GitHub marketplace source (validated + persisted)."""
    from . import marketplace
    p = payload or {}
    result = marketplace.add_source(
        p.get("repo", ""), p.get("subdir", ""), p.get("branch", "main"),
        p.get("kind", "skill"), p.get("target", "hermes"),
        p.get("name", ""), p.get("author", ""),
    )
    if result.get("ok"):
        history_log("marketplace", f"added custom source {p.get('repo', '')}")
        _notify("marketplace", {"action": "source_add", "repo": p.get("repo", "")})
    return result


def api_marketplace_source_remove(payload):
    """Remove a user-added marketplace source by id."""
    from . import marketplace
    p = payload or {}
    result = marketplace.remove_source(p.get("source_id", ""))
    if result.get("ok"):
        history_log("marketplace", f"removed custom source {p.get('source_id', '')}")
    return result


def api_run(payload):
    """POST /api/run — whitelist-only console dispatcher."""
    from . import console
    line = (payload or {}).get("command", "")
    if not line or not line.strip():
        return {"ok": False, "error": "command required", "output": []}
    result = console.run_command(line)
    if result.get("ok"):
        try:
            from . import sse
            sse.hub.broadcast("console", {
                "ts": _ts(), "command": line,
                "output": result.get("output", []), "ok": True,
            })
        except Exception:
            pass
    return result


def _notify(channel: str, data: dict):
    """Fan out an SSE notify frame (best-effort, never raises)."""
    try:
        from . import sse
        sse.hub.broadcast(channel, {**data, "ts": _ts()})
    except Exception:
        pass


def api_failover():
    """Failover status (state + config)."""
    from . import failover
    return {"ok": True, "status": failover.status()}


def api_sessions_search(q=""):
    """Search sessions/messages by content (LIKE, bounded)."""
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "query required"}
    db = _find_state_db()
    if not db.exists():
        return {"ok": False, "error": "state.db not found"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            tables = _tables_in(db)
            msg_tbl = next((t for t in tables if "message" in t.lower()), None)
            if not msg_tbl:
                return {"ok": False, "error": "no messages table"}
            cols = [d[1] for d in con.execute(f"PRAGMA table_info({msg_tbl})")]
            content_col = next((c for c in cols if c.lower() in
                                ("content", "text", "message", "body", "text_content")), None)
            session_col = next((c for c in ("session_id", "session", "conversation_id",
                                            "convo_id") if c in cols), None)
            role_col = next((c for c in cols if "role" in c.lower()), None)
            ts_col = next((c for c in cols if any(
                t in c.lower() for t in ("ts", "time", "date", "created"))), None)
            if not content_col:
                return {"ok": False, "error": "no content column in messages"}
            like = f"%{q}%"
            cur = con.cursor()
            cur.execute(
                f"SELECT {content_col}"
                + (f", {session_col}" if session_col else "")
                + (f", {role_col}" if role_col else "")
                + (f", {ts_col}" if ts_col else "")
                + f" FROM {msg_tbl} WHERE CAST({content_col} AS TEXT) LIKE ? "
                + f"ORDER BY rowid DESC LIMIT 50",
                (like,))
            rows = cur.fetchall()
            names = [d[0] for d in cur.description]
            hits = []
            for r in rows:
                rec = dict(zip(names, r))
                hits.append({
                    "content": str(rec.get(content_col, ""))[:500],
                    "session": str(rec.get(session_col, "")) if session_col else "",
                    "role": str(rec.get(role_col, ""))[:20] if role_col else "",
                    "ts": str(rec.get(ts_col, ""))[:30] if ts_col else "",
                })
            return {"ok": True, "query": q, "hits": hits, "count": len(hits)}
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_sessions_export():
    """Export sessions from state.db as JSON (bounded, small)."""
    db = _find_state_db()
    if not db.exists():
        return {"ok": False, "error": "state.db not found"}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            tables = _tables_in(db)
            session_tbl = next((t for t in tables if "session" in t.lower()), None)
            if not session_tbl:
                return {"ok": False, "error": "no sessions table"}
            cur = con.cursor()
            cur.execute(f"SELECT * FROM {session_tbl} ORDER BY rowid DESC LIMIT 200")
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return {
                "ok": True,
                "export": json.dumps(rows, ensure_ascii=False, default=str),
                "count": len(rows),
                "table": session_tbl,
            }
        finally:
            con.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── v1.4 universal-resource APIs ─────────────────────────────────────────
def _safe(fn, *args, **kwargs):
    """Run fn and return {ok: True, ...} or {ok: False, error} — never raises."""
    try:
        return fn(*args, **kwargs)
    except (ValueError, FileNotFoundError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def api_routing():
    """Task routing hub surface."""
    from . import routing
    return {"ok": True, "enabled": settings.get("routing.enabled", True),
            "default": settings.get("routing.default", "auto"),
            "categories": [{"name": c, "harness": routing.get(c)} for c in routing.categories()]}


def api_routing_set(payload):
    """POST /api/routing/set {category, harness}."""
    from . import routing
    category = (payload or {}).get("category", "")
    harness = (payload or {}).get("harness", "")
    if not category:
        return {"ok": False, "error": "category required"}
    res = _safe(routing.set, category, harness)
    if res.get("ok") is False:
        return res
    history_log("routing", f"{category} -> {harness}")
    return {"ok": True, "routing": api_routing()}


def api_routing_add(payload):
    """POST /api/routing/add {category, harness}."""
    from . import routing
    category = (payload or {}).get("category", "")
    harness = (payload or {}).get("harness", "auto")
    res = _safe(routing.add, category, harness=harness)
    if res.get("ok") is False:
        return res
    history_log("routing", f"added {category} -> {harness}")
    return {"ok": True, "routing": api_routing()}


def api_memory_search(q=""):
    """GET /api/memory/search?q= — RAG note search."""
    from . import memory
    if not q:
        return {"ok": False, "error": "q required"}
    return {"ok": True, "results": memory.search(q)}


def api_memory_notes():
    """GET /api/memory/notes — recent notes."""
    from . import memory
    return {"ok": True, "notes": memory.list(), "stats": memory.stats()}


def api_memory_add(payload):
    """POST /api/memory/add {text, tags}."""
    from . import memory
    text = (payload or {}).get("text", "")
    if not text:
        return {"ok": False, "error": "text required"}
    note_id = memory.add(text, tags=(payload or {}).get("tags") or [])
    history_log("memory", f"note {note_id[:8]}")
    return {"ok": True, "id": note_id}


def api_memory_delete(payload):
    """POST /api/memory/delete {id}."""
    from . import memory
    note_id = (payload or {}).get("id", "")
    res = _safe(memory.delete, note_id)
    if res.get("ok") is False:
        return res
    history_log("memory", f"deleted {note_id[:8]}")
    return {"ok": True}


def api_mcp():
    """Universal MCP registry listing."""
    from . import mcp
    return {"ok": True, "servers": mcp.list_servers(),
            "stats": mcp.stats(), "enabled": settings.get("mcp.enabled", True)}


def api_mcp_adopted():
    """Servers pending/imported adoption (ask-first gate)."""
    from . import mcp
    entries = mcp.list_servers()
    return {"ok": True, "adopted": [e for e in entries if e.get("adopted")],
            "pending": [e for e in entries if not e.get("adopted")]}


def api_mcp_action(payload):
    """POST /api/mcp/* mutations: adopt|add|enable|disable|remove|mode|test|rescan."""
    from . import mcp
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    if action == "rescan":
        res = mcp.rescan()
        history_log("mcp", f"rescan: +{len(res.get('added', []))} found={len(res.get('found', []))}")
        return {"ok": True, **res}
    if action == "adopt":
        res = mcp.adopt((payload or {}).get("names") or "all")
        history_log("mcp", f"adopt {res}")
        return {"ok": True, "adopted": res}
    if action == "add":
        res = _safe(mcp.add, name, (payload or {}).get("type", "stdio"),
                    (payload or {}).get("command", ""), url=(payload or {}).get("url", ""))
        if res.get("ok") is False:
            return res
        history_log("mcp", f"added {name}")
        return {"ok": True, "server": res}
    if action in ("enable", "disable", "remove", "test", "mode"):
        try:
            if action == "enable":
                res = mcp.enable(name)
            elif action == "disable":
                res = mcp.disable(name)
            elif action == "remove":
                res = mcp.remove(name)
            elif action == "test":
                res = mcp.status(name)
            else:
                res = mcp.mode(name, (payload or {}).get("mode", "shared"))
        except (ValueError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        history_log("mcp", f"{action} {name}")
        return {"ok": True, "result": res}
    return {"ok": False, "error": f"unknown mcp action: {action}"}


def api_models_universal():
    """Universal models + assignments."""
    from . import models
    return {"ok": True, "models": models.list_models(), "assignments": models.assignments(),
            "active": {h: models.active(h) for h in ("hermes", "claude", "atropos")}}


def api_models_action(payload):
    """POST /api/models/* mutations: add|remove|assign."""
    from . import models
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    if action == "add":
        res = _safe(models.add, name, model=(payload or {}).get("model", ""),
                    base_url=(payload or {}).get("base_url", ""),
                    api_key_env=(payload or {}).get("api_key_env", ""))
        if res.get("ok") is False:
            return res
        history_log("models", f"added {name}")
        return {"ok": True, "model": res}
    if action == "remove":
        res = _safe(models.remove, name)
        if res.get("ok") is False:
            return res
        history_log("models", f"removed {name}")
        return {"ok": True}
    if action == "assign":
        harness = (payload or {}).get("harness", "")
        res = _safe(models.assign, harness, name)
        if res.get("ok") is False:
            return res
        history_log("models", f"assign {harness} -> {name}")
        return {"ok": True, "models": api_models_universal()}
    return {"ok": False, "error": f"unknown models action: {action}"}


def api_models_toggle(payload):
    """POST /api/models/toggle {name} — toggle enable/disable."""
    from . import models
    name = (payload or {}).get("name", "")
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        data = models._load()
        entry = models._entry(data["entries"], name)
        if entry is None:
            return {"ok": False, "error": f"model not found: {name}"}
        entry["enabled"] = not entry.get("enabled", True)
        models._save(data)
        history_log("models", f"toggle {name} -> {'enabled' if entry['enabled'] else 'disabled'}")
        return {"ok": True, "name": name, "enabled": entry["enabled"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_models_providers():
    """GET /api/models/providers — list providers from router.ROUTERS.

    Only shows providers whose env vars are actually set (or always shows local).
    Users can add custom providers via the dashboard.
    """
    from . import router as _router
    providers = []
    for name, info in _router.ROUTERS.items():
        # Always show local; for others, only if their key env var is set
        if name != "local":
            key_env = info.get("api_key_env", "")
            if key_env and not os.environ.get(key_env):
                continue
        providers.append({
            "name": name,
            "description": info.get("description", ""),
            "base_url": info.get("base_url", ""),
            "api_key_env": info.get("api_key_env", ""),
            "model": info.get("model", ""),
            "model_kinds": info.get("model_kinds", ["chat"]),
        })
    return {"ok": True, "providers": providers}


def api_models_provider_test(payload):
    """POST /api/models/provider/test {name} — ping a provider's /v1/models."""
    from . import router as _router
    name = (payload or {}).get("name", "")
    if not name:
        return {"ok": False, "error": "name required"}
    res = _router.discover_models(name, timeout=10)
    return {"ok": res.get("ok", False), "models": res.get("models", []),
            "count": res.get("count", 0), "error": res.get("error")}


def api_webhooks():
    """Universal webhook registry."""
    from . import webhooks
    return {"ok": True, "webhooks": webhooks.list_webhooks()}


def api_webhooks_action(payload):
    """POST /api/webhooks/* mutations: add|remove|toggle|test."""
    from . import webhooks
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    if action == "add":
        res = _safe(webhooks.add, name, (payload or {}).get("url", ""),
                    (payload or {}).get("events") or ["all"])
        if res.get("ok") is False:
            return res
        history_log("webhooks", f"added {name}")
        return {"ok": True, "webhook": res}
    try:
        if action == "remove":
            webhooks.remove(name)
        elif action == "enable":
            webhooks.enable(name)
        elif action == "disable":
            webhooks.disable(name)
        elif action == "test":
            res = webhooks.ping(name)
            return {"ok": True, "result": res}
        else:
            return {"ok": False, "error": f"unknown webhook action: {action}"}
    except (ValueError, FileNotFoundError) as e:
        return {"ok": False, "error": str(e)}
    history_log("webhooks", f"{action} {name}")
    return {"ok": True}


def api_identity():
    """Universal identity files."""
    from . import identity
    return {"ok": True, "files": identity.list_files(), "stats": identity.stats()}


def api_identity_diff(payload=None):
    """GET /api/identity/diff?name= or POST."""
    from . import identity
    name = (payload or {}).get("name", "") if isinstance(payload, dict) else ""
    if not name:
        return {"ok": False, "error": "name required"}
    return {"ok": True, "name": name, "diffs": identity.diff(name)}


def api_identity_action(payload):
    """POST /api/identity/* mutations: save|mode|sync|restore|conflict."""
    from . import identity
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    if action == "save":
        res = identity.save(name, (payload or {}).get("content", ""))
        history_log("identity", f"saved {name}")
        return {"ok": True, **res}
    if action == "mode":
        res = _safe(identity.mode, name, (payload or {}).get("mode", "shared"))
        if res.get("ok") is False:
            return res
        history_log("identity", f"mode {name}")
        return {"ok": True}
    if action == "sync":
        res = _safe(identity.sync, name)
        if res.get("ok") is False:
            return res
        history_log("identity", f"synced {name}")
        return {"ok": True, "result": res}
    if action == "restore":
        res = _safe(identity.restore, name, int((payload or {}).get("n", 0)))
        if res.get("ok") is False:
            return res
        history_log("identity", f"restored {name}")
        return {"ok": True}
    if action == "conflict":
        res = identity.resolve_conflict(name, (payload or {}).get("target", ""),
                                        (payload or {}).get("action", "keep"))
        history_log("identity", f"conflict {action} {name}")
        return {"ok": True, "result": res}
    return {"ok": False, "error": f"unknown identity action: {action}"}


def api_configs():
    """Universal config manager listing."""
    from . import conflayer
    return {"ok": True, "configs": conflayer.list_configs()}


def api_configs_show(name=""):
    """GET /api/configs/show?name= — file content."""
    from . import conflayer
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        return {"ok": True, "name": name, "content": conflayer.show(name)}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def api_configs_action(payload):
    """POST /api/configs/* mutations: save|mode|rollback|conflict."""
    from . import conflayer
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    if action == "save":
        res = conflayer.save(name, (payload or {}).get("content", ""))
        history_log("configs", f"saved {name}")
        return {"ok": True, **res}
    if action == "mode":
        res = _safe(conflayer.mode, name, (payload or {}).get("mode", "separate"))
        if res.get("ok") is False:
            return res
        history_log("configs", f"mode {name}")
        return {"ok": True}
    if action == "rollback":
        res = _safe(conflayer.rollback, name, int((payload or {}).get("n", 0)))
        if res.get("ok") is False:
            return res
        history_log("configs", f"rolled back {name}")
        return {"ok": True}
    if action == "conflict":
        res = conflayer.resolve_conflict(name, (payload or {}).get("target", ""),
                                         (payload or {}).get("action", "keep"))
        history_log("configs", f"conflict {action} {name}")
        return {"ok": True, "result": res}
    return {"ok": False, "error": f"unknown configs action: {action}"}


def api_audit():
    """Complete-picture resource audit."""
    from . import audit
    return {"ok": True, "table": audit.table(), "summary": audit.summary()}


def api_fleet():
    """Multi-box fleet."""
    from . import fleet
    return {"ok": True, "boxes": fleet.list_boxes()}


def api_fleet_action(payload):
    """POST /api/fleet/* mutations: add|remove|ping."""
    from . import fleet
    action = (payload or {}).get("action", "")
    if action == "add":
        res = _safe(fleet.add, (payload or {}).get("name", ""),
                    (payload or {}).get("url", ""), (payload or {}).get("token", ""))
        if res.get("ok") is False:
            return res
        history_log("fleet", f"added {payload.get('name', '')}")
        return {"ok": True}
    if action == "remove":
        fleet.remove((payload or {}).get("id", ""))
        history_log("fleet", "removed box")
        return {"ok": True}
    if action == "ping":
        rows = fleet.ping((payload or {}).get("id", "") or "all")
        return {"ok": True, "results": rows}
    return {"ok": False, "error": f"unknown fleet action: {action}"}


def api_budget():
    """Usage & quota gate status."""
    from . import budget
    return {"ok": True, "usage": budget.usage()}


def api_budget_check():
    """POST /api/budget/check — run the gate (alert + optional failover)."""
    from . import budget
    res = budget.check_and_alert()
    history_log("budget", f"check: {res}")
    return {"ok": True, "result": res}


def api_links():
    """One-shot share links."""
    from . import links
    return {"ok": True, "links": links.list_links()}


def api_links_action(payload):
    """POST /api/links/* mutations: create|revoke."""
    from . import links
    action = (payload or {}).get("action", "")
    if action == "create":
        l = links.create((payload or {}).get("session_id", ""))
        history_log("links", f"created share for {l.get('session_id')}")
        return {"ok": True, "link": l}
    if action == "revoke":
        links.revoke((payload or {}).get("token", ""))
        history_log("links", "revoked link")
        return {"ok": True}
    return {"ok": False, "error": f"unknown links action: {action}"}


def api_snapshots():
    """Snapshot gallery."""
    from . import snapshots
    return {"ok": True, "snapshots": snapshots.list_snapshots()}


def api_snapshots_action(payload):
    """POST /api/snapshots/* mutations: create|restore."""
    from . import snapshots
    action = (payload or {}).get("action", "")
    if action == "create":
        res = snapshots.create((payload or {}).get("label", "manual"))
        history_log("snapshots", f"created {res.get('name', '')}")
        return {"ok": True, "snapshot": res}
    if action == "restore":
        res = _safe(snapshots.restore, (payload or {}).get("name", ""))
        if res.get("ok") is False:
            return res
        history_log("snapshots", f"restored {payload.get('name', '')}")
        return {"ok": True, "result": res}
    return {"ok": False, "error": f"unknown snapshots action: {action}"}


def api_activity():
    """24h activity timeline."""
    from . import activity
    return {"ok": True, "feed": activity.feed()}


def api_files_list(path=""):
    """GET /api/files/list?path= — repo file listing (read-only, safe)."""
    from . import files
    res = files.list_dir(path or None)
    if not res.get("ok"):
        return res
    return {"ok": True, "root": res.get("root"), "entries": res["entries"]}


def api_files_read(path=""):
    """GET /api/files/read?path= — read a text file (bounded)."""
    from . import files
    if not path:
        return {"ok": False, "error": "path required"}
    return files.read_file(path)


def api_files_search(q=""):
    """GET /api/files/search?q= — filename search."""
    from . import files
    if not q:
        return {"ok": False, "error": "q required"}
    return {"ok": True, "results": files.search(q=q)}


def api_announce():
    """Announcement feed (tips + changelog + version check)."""
    from . import notify
    return {"ok": True, "feed": notify.feed()}


def api_announce_dismiss(payload):
    """POST /api/announce/dismiss {id}."""
    from . import notify
    notify.dismiss((payload or {}).get("id", ""))
    return {"ok": True}


def api_chat_sessions():
    """Mobile chat sessions."""
    from . import chat
    return {"ok": True, "sessions": chat.session_list(), "stats": chat.stats()}


def api_chat_session(session_id=""):
    """GET /api/chat/session/<id> — messages of one session."""
    from . import chat
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    try:
        return {"ok": True, "session_id": session_id,
                "messages": chat.session_messages(session_id)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_chat_verify_share(token=""):
    """GET /api/chat/verify-share?token= — one-shot share link consumer."""
    from . import links
    if not token:
        return {"ok": False, "error": "token required"}
    res = links.verify(token)
    if not res.get("ok"):
        return {"ok": False, "error": "invalid or used share link"}
    return {"ok": True, "session_id": res.get("session_id")}


def api_chat_send(payload):
    """POST /api/chat/send {session_id?, text, harness?, effort?}."""
    from . import chat
    text = (payload or {}).get("text", "")
    if not text:
        return {"ok": False, "error": "text required"}
    res = chat.send((payload or {}).get("session_id"), text,
                    harness=(payload or {}).get("harness"),
                    effort=(payload or {}).get("effort"))
    if res.get("ok"):
        try:
            from . import sse
            sse.hub.broadcast("chat", {"ts": _ts(), "session_id": res.get("session_id"),
                                       "harness": res.get("harness")})
        except Exception:
            pass
        history_log("chat", f"send to {res.get('harness', '?')}")
    return res


def api_chat_delete(payload):
    """POST /api/chat/delete {session_id}."""
    from . import chat
    res = _safe(chat.delete_session, (payload or {}).get("session_id", ""))
    if res.get("ok") is False:
        return res
    history_log("chat", "deleted session")
    return {"ok": True}


def api_chat_action(payload):
    """POST /api/chat/action — session housekeeping from the mobile chat.

    Actions: rename {session_id, title}, pin {session_id, pinned},
    tag {session_id, tag, remove?}, delete_message {message_id}.
    """
    from . import chat
    action = (payload or {}).get("action", "")
    sid = (payload or {}).get("session_id", "")
    if action == "rename":
        title = chat.rename_session(sid, (payload or {}).get("title", ""))
        return {"ok": True, "title": title}
    if action == "pin":
        return {"ok": bool(chat.pin_session(sid, bool((payload or {}).get("pinned", True))))}
    if action == "tag":
        tag = (payload or {}).get("tag", "")
        if not tag:
            return {"ok": False, "error": "tag required"}
        if (payload or {}).get("remove"):
            return {"ok": bool(chat.remove_tag(sid, tag))}
        return {"ok": bool(chat.tag_session(sid, tag))}
    if action == "delete_message":
        mid = (payload or {}).get("message_id")
        if mid is None:
            return {"ok": False, "error": "message_id required"}
        return {"ok": bool(chat.delete_message(mid))}
    return {"ok": False, "error": f"unknown action: {action}"}


def api_chat_share(payload):
    """POST /api/chat/share {session_id} — create a one-shot share link."""
    from . import links
    try:
        return {"ok": True, "link": links.create((payload or {}).get("session_id", ""))}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def api_chat_export(payload):
    """POST /api/chat/export {session_id} — JSONL."""
    from . import chat
    try:
        return {"ok": True, "jsonl": chat.export((payload or {}).get("session_id", ""))}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def api_lan_share():
    """LAN share card: URL + IP + port + real scannable QR (ascii)."""
    from . import lan
    return {"ok": True, "url": lan.share_url(), "ip": lan.lan_ip(),
            "port": settings.get("dashboard.port", 8787),
            "qr": lan.qr_ascii(lan.share_url())}


def api_devices():
    """Pairing/device approval flow."""
    from . import lan
    return {"ok": True, "devices": lan.known_devices()}


def api_devices_action(payload):
    """POST /api/devices/* mutations: approve|deny."""
    from . import lan
    action = (payload or {}).get("action", "")
    device_id = (payload or {}).get("id", "")
    try:
        if action == "approve":
            lan.approve(device_id)
        elif action == "deny":
            lan.deny(device_id)
        else:
            return {"ok": False, "error": f"unknown devices action: {action}"}
    except (ValueError, FileNotFoundError) as e:
        return {"ok": False, "error": str(e)}
    history_log("devices", f"{action} {device_id}")
    return {"ok": True, "devices": lan.known_devices()}


def api_commands():
    """Universal commands & aliases."""
    from . import commands
    return {"ok": True, **commands.list()}


def api_commands_action(payload):
    """POST /api/commands/* mutations: add|remove|alias."""
    from . import commands
    action = (payload or {}).get("action", "")
    name = (payload or {}).get("name", "")
    if action == "add":
        res = _safe(commands.add_command, name, (payload or {}).get("template", ""),
                    (payload or {}).get("description", ""))
        if res.get("ok") is False:
            return res
        history_log("commands", f"added {name}")
        return {"ok": True}
    if action == "remove":
        res = _safe(commands.remove_command, name)
        if res.get("ok") is False:
            return res
        history_log("commands", f"removed {name}")
        return {"ok": True}
    if action == "alias":
        res = _safe(commands.add_alias, name, (payload or {}).get("target", ""))
        if res.get("ok") is False:
            return res
        history_log("commands", f"alias {name}")
        return {"ok": True}
    return {"ok": False, "error": f"unknown commands action: {action}"}


# ── v1.4 final polish: QR / sync / backup multi-backend / update AI / wizard ─
def api_qr():
    """Real scannable QR for the dashboard URL (matrix + svg + ascii)."""
    try:
        from . import qr
        from . import lan
        url = lan.share_url()
        matrix = qr.qr_matrix(url)
        return {"ok": True, "url": url, "modules": len(matrix),
                "svg": qr.qr_svg(url),
                "ascii": qr.qr_ascii(url),
                "version": "1-4"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _api_capabilities():
    """GET /api/capabilities — probe all harnesses for their features."""
    try:
        from . import probe
        caps = probe.probe_capabilities()
        registry = {k: v for k, v in probe.SECTION_REQUIRES.items()}
        shown = probe.available_sections(registry, caps)
        return {"ok": True, "capabilities": caps, "sections": registry,
                "shown": shown}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _api_telegram_status():
    """GET /api/telegram/status — gateway configuration state."""
    try:
        from . import telegram
        return {"ok": True, "status": telegram.status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _api_agents_status():
    """GET /api/agents — agent definitions + recent run count."""
    from . import agents
    return {"ok": True, "agents": agents.list_agents(), "runs": len(agents.recent_runs())}


def api_agents_action(payload):
    """POST /api/agents/run — run an agent with a task."""
    from . import agents
    name = (payload or {}).get("name", "")
    task = (payload or {}).get("task", "")
    try:
        rec = agents.run_agent(name, task)
        return {"ok": bool(rec.get("ok")), **rec}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_middleware_action(payload):
    """POST /api/middleware/<action> — list/toggle Filters & Plugins."""
    from . import middleware
    action = (payload or {}).get("action", "list")
    try:
        if action == "list":
            rows = [[k, v["description"], "on" if k in middleware.enabled_list() else "off"]
                    for k, v in middleware.catalog().items()]
            return {"ok": True, "filters": rows, "enabled": middleware.enabled_list()}
        if action == "on":
            for name in (payload or {}).get("names") or [(payload or {}).get("name") or ""]:
                if name:
                    middleware.set_enabled(name, True)
            return {"ok": True, "enabled": middleware.enabled_list()}
        if action == "off":
            for name in (payload or {}).get("names") or [(payload or {}).get("name") or ""]:
                if name:
                    middleware.set_enabled(name, False)
            return {"ok": True, "enabled": middleware.enabled_list()}
        if action == "order":
            middleware.set_order((payload or {}).get("names") or [])
            return {"ok": True, "enabled": middleware.enabled_list()}
        return {"ok": False, "error": f"unknown middleware action: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_wizard_status():
    """Setup wizard state: already-imported resources + detected harnesses."""
    try:
        from . import setup_wizard as wz
        return {"ok": True, **wz.discover_summary()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_sync_status():
    """Devices/sync panel: peers, pending, backends."""
    try:
        from . import sync as sync_mod
        return {"ok": True, **sync_mod.sync_status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_sync_action(payload):
    """POST /api/sync/* — push|pull|host-pair|join|status."""
    try:
        from . import sync as sync_mod
        action = (payload or {}).get("action", "")
        backend = (payload or {}).get("backend", "file")
        if action == "push":
            out = sync_mod.sync_push(backend, target=(payload or {}).get("target"))
            history_log("sync", f"push {backend}")
            return {"ok": True, **out}
        if action == "pull":
            out = sync_mod.sync_pull(backend, source=(payload or {}).get("source"))
            history_log("sync", f"pull {backend}")
            return {"ok": True, **out}
        if action == "host-pair":
            return {"ok": True, **sync_mod.host_pair()}
        if action == "join":
            return sync_mod.join_pair((payload or {}).get("code", ""))
        if action == "status":
            return {"ok": True, **sync_mod.sync_status()}
        return {"ok": False, "error": f"unknown sync action: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_backup_backends():
    """Multi-backend backup status: configured/connected per backend."""
    try:
        from . import backup as backup_mod
        return {"ok": True, "backends": backup_mod.list_backends()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_backup_backend_action(payload):
    """POST /api/backup/backend — backup-now to a backend / restore / preview."""
    try:
        from . import backup as backup_mod
        action = (payload or {}).get("action", "")
        backend = (payload or {}).get("backend", "file")
        if action == "create":
            result = backup_mod.create_backend(backend)
            history_log("backup", f"backend {backend}: {result.get('ok')}")
            return result
        if action == "restore":
            name = (payload or {}).get("name", "")
            if not name:
                return {"ok": False, "error": "name required"}
            prev = backup_mod.restore_preview(name)
            if not (payload or {}).get("confirm"):
                return {"ok": True, "preview": prev, "confirm": True}
            result = backup_mod.restore_backend(backend, name)
            history_log("backup", f"restore {backend}:{name}")
            return result
        if action == "preview":
            name = (payload or {}).get("name", "")
            return {"ok": True, "preview": backup_mod.restore_preview(name)}
        if action == "retention":
            from . import settings as _st
            _st.set("backup.retention", (payload or {}).get("keep", 5))
            _st.set("backup.retention_weekly", (payload or {}).get("weekly", 4))
            return {"ok": True}
        return {"ok": False, "error": f"unknown backup action: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_wizard_import(payload):
    """POST /api/wizard/import — import a resource group from a harness."""
    try:
        from . import setup_wizard as wz
        group = (payload or {}).get("group", "")
        harness = (payload or {}).get("harness", "claude")
        mode = (payload or {}).get("mode", "shared")
        return wz._import_group(group, [harness], mode=mode)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_update_ai_status():
    """AI update engine: history + current status + config."""
    try:
        from . import update_ai
        return {"ok": True,
                "history": update_ai.load_history().get("attempts", [])[-20:],
                "mode": settings.get("update-ai.mode"),
                "model": settings.get("update-ai.model"),
                "effort": settings.get("update-ai.effort"),
                "update_auto": settings.get("update.auto"),
                "update_auto_ai": settings.get("update.auto_ai")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_update_ai_action(payload):
    """POST /api/update-ai/* — check|apply|config."""
    try:
        from . import update_ai
        action = (payload or {}).get("action", "")
        if action == "check":
            return {"ok": True, **update_ai.ai_check(update_ai.failed_patch_state())}
        if action == "apply":
            attempt_id = (payload or {}).get("attempt_id", "")
            confirm = bool((payload or {}).get("confirm", False))
            return update_ai.apply_ai(attempt_id, confirm=confirm)
        if action == "config":
            mode = (payload or {}).get("mode")
            model = (payload or {}).get("model")
            effort = (payload or {}).get("effort")
            if mode:
                settings.set("update-ai.mode", mode)
            if model:
                settings.set("update-ai.model", model)
            if effort:
                settings.set("update-ai.effort", effort)
            history_log("update-ai", f"config mode={mode} model={model}")
            return {"ok": True}
        if action == "set-auto":
            value = (payload or {}).get("value", "off")
            settings.set("update.auto", value)
            settings.set("update.auto_ai", bool((payload or {}).get("ai", False)))
            history_log("update", f"auto={value} ai={bool((payload or {}).get('ai', False))}")
            return {"ok": True}
        return {"ok": False, "error": f"unknown update-ai action: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── HTTP handler ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        # Auth disabled — dashboard is bound to localhost, no token needed
        return True

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
        # dashboard static assets (sw.js for PWA offline, icon for header/favicon)
        if path in ("/sw.js", "/manifest.webmanifest", "/chat.html", "/icon.png",
                    "/icon-256.png", "/favicon.png"):
            f = DASHBOARD_DIR / (path.lstrip("/").replace("favicon.png", "icon-256.png"))
            if f.exists():
                ctype = ("application/javascript; charset=utf-8" if path.endswith(".js")
                         else "application/manifest+json" if path.endswith(".webmanifest")
                         else "image/png" if path.endswith(".png")
                         else "text/html; charset=utf-8")
                self._send(200, f.read_bytes(), ctype)
                return True
        if not path.startswith("/api/"):
            return None
        api = {
            "/api/status": api_status,
            "/api/version": api_version,
            "/api/doctor": lambda: api_doctor(),
            "/api/patches": api_patches,
            "/api/route": lambda: api_route(),
            "/api/router": lambda: api_route(),
            "/api/router/models": api_router_models,
            "/api/guest": api_guest,
            "/api/sessions": api_sessions,
            "/api/models": api_models,
            "/api/cron": api_cron,
            "/api/skills": api_skills,
            "/api/skills/usage": api_autoskill_usage,
            "/api/hermes/skills": api_hermes_skills,
            "/api/plugins": api_plugins,
            "/api/channels": api_channels,
            "/api/hermes-config": api_hermes_config,
            "/api/analytics": api_analytics,
            "/api/files": api_files,
            "/api/backup": lambda: api_backup_list(),
            "/api/backup/list": api_backup_list,
            "/api/history": lambda: api_history(),
            "/api/claude": api_claude,
            "/api/claude/settings": lambda: api_claude_settings(),
            "/api/config": lambda: api_config_get(),
            "/api/effort": api_effort,
            "/api/update/check": lambda: api_update(check=True),
            "/api/update/version": api_update_version,
            "/api/update/state": lambda: api_update_state(),
            "/api/changelog": api_changelog,
            "/api/router/history": api_router_history,
            "/api/analytics/cost": api_analytics_cost,
            "/api/backup/config": api_backup_config,
            "/api/claude/doctor": api_claude_doctor,
            # New: self-heal / alert / jailbreak
            "/api/self-heal": lambda: api_self_heal(),
            "/api/alert/test": lambda: api_alert_test(),
            "/api/alert/check": lambda: api_alert_check(),
            "/api/jailbreak": lambda: api_jailbreak_status(),
            "/api/jailbreak/apply": lambda: api_jailbreak_apply(),
            "/api/jailbreak/apply-all": lambda: api_jailbreak_apply_all(),
            "/api/jailbreak/revert": lambda: api_jailbreak_revert(),
            # v1.2.0
            "/api/settings": api_settings,
            "/api/settings/export": api_settings_export,
            "/api/extensions": lambda: api_extensions("all"),
            "/api/extensions/skills": lambda: api_extensions("skill"),
            "/api/extensions/plugins": lambda: api_extensions("plugin"),
            "/api/marketplace": api_marketplace,
            "/api/failover": api_failover,
            "/api/sessions/export": api_sessions_export,
            # v1.4 universal resources
            "/api/routing": api_routing,
            "/api/memory/notes": api_memory_notes,
            "/api/mcp": api_mcp,
            "/api/mcp/adopted": api_mcp_adopted,
            "/api/models": api_models_universal,
            "/api/webhooks": api_webhooks,
            "/api/identity": api_identity,
            "/api/configs": api_configs,
            "/api/audit": api_audit,
            "/api/fleet": api_fleet,
            "/api/budget": api_budget,
            "/api/links": api_links,
            "/api/snapshots": api_snapshots,
            "/api/activity": api_activity,
            "/api/announce": api_announce,
            "/api/chat/sessions": api_chat_sessions,
            "/api/lan/share": api_lan_share,
            "/api/devices": api_devices,
            "/api/commands": api_commands,
            # v1.4 final polish
            "/api/qr": api_qr,
            "/api/sync": api_sync_status,
            "/api/sync/status": api_sync_status,
            "/api/backup/backends": api_backup_backends,
            "/api/update-ai": api_update_ai_status,
            "/api/wizard/status": api_wizard_status,
            "/api/middleware": lambda: api_middleware_action({"action": "list"}),
            "/api/middleware/list": lambda: api_middleware_action({"action": "list"}),
            "/api/agents": lambda: _api_agents_status(),
            "/api/capabilities": lambda: _api_capabilities(),
            "/api/telegram/status": lambda: _api_telegram_status(),
            "/api/dashboard/auto-status": lambda: auto_status(),
            "/api/models/providers": lambda: api_models_providers(),
            "/api/models/universal": lambda: api_models_universal(),
        }
        if path == "/api/memory/search":
            return api_memory_search((q.get("q") or [""])[0])
        if path == "/api/files/list":
            return api_files_list((q.get("path") or [""])[0])
        if path == "/api/files/read":
            return api_files_read((q.get("path") or [""])[0])
        if path == "/api/files/search":
            return api_files_search((q.get("q") or [""])[0])
        if path == "/api/identity/diff":
            return api_identity_diff({"name": (q.get("name") or [""])[0]})
        if path == "/api/configs/show":
            return api_configs_show((q.get("name") or [""])[0])
        if path == "/api/chat/stats":
            return api_chat_sessions()
        if path.startswith("/api/chat/session/"):
            sid = path.split("/api/chat/session/", 1)[1]
            return api_chat_session(sid)
        if path == "/api/chat/verify-share":
            return api_chat_verify_share((q.get("token") or [""])[0])
        if path in api:
            return api[path]()
        if path == "/api/logs":
            tail = int((q.get("tail") or ["80"])[0])
            return api_logs(tail=tail)
        if path in ("/api/route/test", "/api/router/test"):
            return api_route_test()
        if path.startswith("/api/config/"):
            raw_key = path.split("/api/config/", 1)[1]
            return api_config_get(raw_key)
        if path == "/api/guest/persona":
            return api_guest_persona(method="GET")
        # /api/cron/{id}/pause|resume  (GET is a no-op read; POST does the mutation)
        if path.startswith("/api/cron/"):
            parts = path.split("/api/cron/", 1)[1].split("/")
            if len(parts) == 2 and parts[1] in ("pause", "resume"):
                return api_cron_toggle(parts[0], parts[1] == "resume")
        # /api/session/{id}  — trace drill-down
        if path.startswith("/api/session/"):
            sid = path.split("/api/session/", 1)[1]
            return api_session_trace(sid)
        # v1.2.0 session search
        if path.startswith("/api/sessions/search"):
            qq = (q.get("q") or [""])[0]
            return api_sessions_search(qq)
        return None  # unknown → 404

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
            return api_backup_create()
        if path == "/api/backup/config":
            return api_backup_config_set(payload)
        if path == "/api/patches/apply":
            return api_patches_apply()
        if path == "/api/route/apply":
            return api_route_apply()
        if path == "/api/claude/settings":
            return api_claude_settings(payload.get("content"))
        if path == "/api/claude/doctor":
            return api_claude_doctor()
        if path == "/api/auth":
            return api_auth_check(payload)
        if path == "/api/hermes-config":
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
        if path == "/api/effort/set":
            return api_effort_set(payload)
        if path == "/api/backup/create":
            return api_backup_create()
        if path in ("/api/backup/now",):
            return api_backup_create()
        if path in ("/api/route/set", "/api/router/set"):
            return api_route("set", payload.get("name"))
        if path in ("/api/route/test", "/api/router/test"):
            return api_route_test(payload.get("name"))
        if path in ("/api/route/apply", "/api/router/apply"):
            return api_route_apply()
        if path == "/api/update/check":
            return api_update(check=True)
        if path in ("/api/cron/pause",):
            return api_cron_toggle(payload.get("job_id", ""), False)
        if path in ("/api/cron/resume",):
            return api_cron_toggle(payload.get("job_id", ""), True)
        # generic: /api/cron/{id}/pause  /api/cron/{id}/resume  via POST body
        if path.startswith("/api/cron/"):
            parts = path.split("/api/cron/", 1)[1].split("/")
            if len(parts) == 2 and parts[1] in ("pause", "resume"):
                return api_cron_toggle(parts[0], parts[1] == "resume")
        # v1.2.0
        if path == "/api/settings/set":
            return api_settings_set(payload)
        if path == "/api/settings/import":
            return api_settings_import(payload)
        if path == "/api/extensions/action":
            return api_extension_action(payload)
        if path == "/api/marketplace/install":
            return api_marketplace_install(payload)
        if path == "/api/marketplace/uninstall":
            return api_marketplace_uninstall(payload)
        if path == "/api/marketplace/source/add":
            return api_marketplace_source_add(payload)
        if path == "/api/marketplace/source/remove":
            return api_marketplace_source_remove(payload)
        if path == "/api/run":
            return api_run(payload)
        # v1.4 universal-resource mutations
        if path == "/api/routing/set":
            return api_routing_set(payload)
        if path == "/api/routing/add":
            return api_routing_add(payload)
        if path == "/api/memory/add":
            return api_memory_add(payload)
        if path == "/api/memory/delete":
            return api_memory_delete(payload)
        if path.startswith("/api/mcp/"):
            return api_mcp_action({**payload, "action": path.split("/api/mcp/", 1)[1]})
        if path.startswith("/api/models/"):
            return api_models_action({**payload, "action": path.split("/api/models/", 1)[1]})
        if path.startswith("/api/webhooks/"):
            return api_webhooks_action({**payload, "action": path.split("/api/webhooks/", 1)[1]})
        if path == "/api/identity/save" or path.startswith("/api/identity/"):
            return api_identity_action({**payload, "action": path.split("/api/identity/", 1)[1]})
        if path.startswith("/api/configs/"):
            return api_configs_action({**payload, "action": path.split("/api/configs/", 1)[1]})
        if path == "/api/fleet/add" or path.startswith("/api/fleet/"):
            return api_fleet_action({**payload, "action": path.split("/api/fleet/", 1)[1]})
        if path == "/api/budget/check":
            return api_budget_check()
        if path == "/api/links/create" or path.startswith("/api/links/"):
            return api_links_action({**payload, "action": path.split("/api/links/", 1)[1]})
        if path.startswith("/api/snapshots/"):
            return api_snapshots_action({**payload, "action": path.split("/api/snapshots/", 1)[1]})
        if path == "/api/announce/dismiss":
            return api_announce_dismiss(payload)
        if path == "/api/chat/send":
            return api_chat_send(payload)
        if path == "/api/chat/delete":
            return api_chat_delete(payload)
        if path == "/api/chat/action":
            return api_chat_action(payload)
        if path == "/api/chat/share":
            return api_chat_share(payload)
        if path == "/api/chat/export":
            return api_chat_export(payload)
        if path == "/api/devices/approve" or path.startswith("/api/devices/"):
            return api_devices_action({**payload, "action": path.split("/api/devices/", 1)[1]})
        if path == "/api/commands/add" or path.startswith("/api/commands/"):
            return api_commands_action({**payload, "action": path.split("/api/commands/", 1)[1]})
        if path == "/api/sync/action" or path.startswith("/api/sync/"):
            sub = path.split("/api/sync/", 1)[1]
            merged = dict(payload or {})
            if sub != "action":
                merged["action"] = sub
            return api_sync_action(merged)
        if path == "/api/backup/backend" or path.startswith("/api/backup/backend/"):
            return api_backup_backend_action({**payload, "action": path.split("/api/backup/backend/", 1)[1]})
        if path == "/api/update-ai/action" or path.startswith("/api/update-ai/"):
            sub = path.split("/api/update-ai/", 1)[1]
            merged = dict(payload or {})
            if sub != "action":
                merged["action"] = sub
            return api_update_ai_action(merged)
        if path == "/api/wizard/import":
            return api_wizard_import(payload)
        if path == "/api/middleware" or path.startswith("/api/middleware/"):
            sub = path.split("/api/middleware/", 1)[1] if path.startswith("/api/middleware/") else "list"
            return api_middleware_action({**(payload or {}), "action": sub})
        if path == "/api/agents/run":
            return api_agents_action(payload)
        if path == "/api/dashboard/auto-start":
            return auto_start(int((payload or {}).get("port", 8787)))
        if path == "/api/dashboard/auto-stop":
            return auto_stop()
        if path == "/api/models/toggle":
            return api_models_toggle(payload)
        if path == "/api/models/provider/test":
            return api_models_provider_test(payload)
        # v19 M5: Session Engine endpoints
        if path == "/api/session_engine/config":
            return api_session_engine_config(payload)
        if path == "/api/session_engine/stats":
            return api_session_engine_stats()
        if path == "/api/sessions/route":
            return api_sessions_route(payload)
        if path == "/api/sessions/merge":
            return api_sessions_merge(payload)
        if path == "/api/sessions/detailed":
            return api_sessions_list_detailed(payload)
        if path == "/api/session_engine/explain":
            return api_session_engine_explain(payload)
        return {"ok": False, "error": f"unknown api: {path}"}

    def do_GET(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path in ("/", "/index.html"):
            self._route_get(path, q)
            return
        # dashboard static assets (sw.js for PWA offline)
        if path in ("/sw.js", "/manifest.webmanifest", "/chat.html"):
            self._route_get(path, q)
            return
        if path == "/api/logs/stream":
            self.do_GET_sse(path, q)
            return
        if path == "/api/events":
            self.do_GET_events(path, q)
            return
        if path == "/health":
            # v18 B.4 — unauthenticated liveness probe (Railway healthcheck)
            data = api_health()
            body, ctype, status = _body(200, data)
            self._send(status, body, ctype)
            return
        if path.startswith("/api/"):
            if not self._auth():
                self._send(401, b'{"error":"unauthorized"}')
                return
            try:
                data = self._route_get(path, q)
            except Exception as e:
                # structured error envelope instead of a stack trace
                data = _error("internal", f"internal error: {e}", status=500)
            if data is not None:
                body, ctype, status = _body(200, data)
                self._send(status, body, ctype)
                return
            self._send(404, b'{"error":"not found"}')
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/auth" and not self._auth():
            self._send(401, b'{"error":"unauthorized"}')
            return
        if path == "/api/chat/stream":
            self.do_chat_stream()
            return
        payload = self._read_json()
        try:
            data = self._route_post(path, payload)
        except Exception as e:
            # structured error envelope instead of a stack trace
            data = _error("internal", f"internal error: {e}", status=500)
        body, ctype, status = _body(200, data)
        self._send(status, body, ctype)

    def do_chat_stream(self):
        """SSE stream for the mobile chat: POST /api/chat/stream.

        Body: {session_id?, text, harness?, effort?}. Frames:
        data: {"event":"delta","text":...} ... data: {"event":"done","session_id":...}
        """
        try:
            payload = self._read_json()
        except Exception:
            payload = {}
        text = (payload or {}).get("text", "")
        if not text:
            self._send(400, b'{"error":"text required"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            from . import chat
            gen = chat.chat_stream((payload or {}).get("session_id"), text,
                                   harness=(payload or {}).get("harness"))
            for frame in gen:
                self._send_sse(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            try:
                self._send_sse({"event": "error", "error": str(e)})
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Atropos-Token")
        self.end_headers()

    def _send_sse(self, data):
        """Send one SSE frame. data is a JSON-serializable dict."""
        try:
            self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            raise

    def do_GET_sse(self, path, q):
        """SSE streaming of gateway logs for /api/logs/stream."""
        if not self._auth():
            self._send(401, b'{"error":"unauthorized"}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        tail = int((q.get("tail") or ["80"])[0])
        interval = float((q.get("interval") or ["2"])[0])
        try:
            # Snapshot: last `tail` lines.
            name, lines = logs.tail(tail)
            try:
                self._send_sse({"event": "snapshot", "file": name or "", "lines": lines})
            except Exception:
                return
            # Follow: offset-based append tracking on the same file.
            target = logs.latest_log_file()
            offset = 0
            if target:
                try:
                    offset = target.stat().st_size
                except Exception:
                    offset = 0
            while True:
                time.sleep(interval)
                cur = logs.latest_log_file()
                if cur is None:
                    continue
                # File switched → resend snapshot.
                if target is None or str(cur.resolve()) != str(target.resolve()):
                    target = cur
                    name, lines = logs.tail(tail)
                    offset = target.stat().st_size
                    try:
                        self._send_sse({"event": "snapshot", "file": name or "", "lines": lines})
                    except Exception:
                        return
                    continue
                try:
                    size = cur.stat().st_size
                except Exception:
                    continue
                if size <= offset:
                    # Truncated/rotated → restart from beginning.
                    if size < offset:
                        offset = 0
                    else:
                        continue
                try:
                    with open(cur, "rb") as f:
                        f.seek(offset)
                        chunk = f.read(size - offset).decode("utf-8", errors="replace")
                except Exception:
                    continue
                offset = size
                new_lines = [l for l in chunk.splitlines() if l != ""]
                if new_lines:
                    try:
                        self._send_sse({"event": "lines", "file": cur.name, "lines": new_lines})
                    except Exception:
                        return
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

    def do_GET_events(self, path, q):
        """SSE hub feed for all panels (/api/events). Token via query."""
        if not self._auth():
            self._send(401, b'{"error":"unauthorized"}')
            return
        from . import sse
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        client_id = f"ev-{secrets.token_urlsafe(8)}"
        # SSE Last-Event-ID resume: replay missed frames for reconnects.
        last_event_id = None
        try:
            raw = int(self.headers.get("Last-Event-ID", "") or "")
            last_event_id = raw if raw > 0 else None
        except (ValueError, TypeError):
            last_event_id = None
        try:
            for frame in sse.stream(client_id, last_event_id=last_event_id):
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass  # quiet


# ── auto-start (OS-level) ─────────────────────────────────────────────────
_TASK_NAME = "AtroposDashboard"


def _kill_stale_port(port: int):
    """Kill any process already listening on *port* (best-effort)."""
    try:
        if os.name == "nt":
            import subprocess as _sp
            out = _sp.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    _sp.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=5)
                    break
        else:
            import subprocess as _sp
            out = _sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5)
            for pid in out.stdout.strip().splitlines():
                if pid.isdigit():
                    _sp.run(["kill", "-9", pid], capture_output=True, timeout=5)
    except Exception:
        pass


def auto_start(port: int = 8787) -> dict:
    """Register an OS-level auto-start task for the dashboard.

    Windows: Task Scheduler (at logon).  Linux/macOS: cron @reboot.
    Returns ``{ok, message}``.
    """
    import sys as _sys
    exe = _sys.executable
    script = str(Path(__file__).resolve().parent.parent / "atropos")
    if os.name == "nt":
        import subprocess as _sp
        cmd = f'"{exe}" "{script}" dashboard --port {port}'
        # Create via schtasks (runs at logon, highest privilege)
        xml = (
            f'<?xml version="1.0" encoding="UTF-16"?>'
            f'<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f'<Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>'
            f'<Principals><Principal><LogonType>InteractiveToken</LogonType>'
            f'<RunLevel>HighestAvailable</RunLevel></Principal></Principals>'
            f'<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
            f'<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
            f'<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries></Settings>'
            f'<Actions><Exec><Command>{exe}</Command>'
            f'<Arguments>"{script}" dashboard --port {port}</Arguments></Exec></Actions>'
            f'</Task>'
        )
        xml_path = detect.atropos_home() / "_task.xml"
        xml_path.write_text(xml, encoding="utf-16")
        r = _sp.run(["schtasks", "/Create", "/TN", _TASK_NAME, "/XML", str(xml_path),
                      "/F"], capture_output=True, text=True, timeout=15)
        xml_path.unlink(missing_ok=True)
        if r.returncode == 0:
            return {"ok": True, "message": f"auto-start registered (Task Scheduler: {_TASK_NAME})"}
        return {"ok": False, "error": r.stderr.strip() or "schtasks failed"}
    else:
        # Linux/macOS: write a cron @reboot entry
        import subprocess as _sp
        cron_line = f"@reboot {exe} {script} dashboard --port {port} &"
        existing = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = [l for l in existing.stdout.splitlines() if "atropos" not in l.lower()]
        lines.append(cron_line)
        proc = _sp.run(["crontab", "-"], input="\n".join(lines) + "\n",
                        capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return {"ok": True, "message": "auto-start registered (cron @reboot)"}
        return {"ok": False, "error": proc.stderr.strip() or "crontab failed"}


def auto_stop() -> dict:
    """Remove the OS-level auto-start task."""
    import sys as _sys
    if os.name == "nt":
        import subprocess as _sp
        r = _sp.run(["schtasks", "/Delete", "/TN", _TASK_NAME, "/F"],
                      capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return {"ok": True, "message": "auto-start removed"}
        return {"ok": False, "error": r.stderr.strip() or "not found"}
    else:
        import subprocess as _sp
        existing = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = [l for l in existing.stdout.splitlines()
                 if "atropos" not in l.lower() or "dashboard" not in l.lower()]
        _sp.run(["crontab", "-"], input="\n".join(lines) + "\n",
                 capture_output=True, text=True, timeout=5)
        return {"ok": True, "message": "auto-start removed (cron)"}


def auto_status() -> dict:
    """Check if auto-start is registered."""
    if os.name == "nt":
        import subprocess as _sp
        r = _sp.run(["schtasks", "/Query", "/TN", _TASK_NAME],
                      capture_output=True, text=True, timeout=10)
        return {"ok": r.returncode == 0, "registered": r.returncode == 0,
                "message": "registered" if r.returncode == 0 else "not registered"}
    else:
        import subprocess as _sp
        r = _sp.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        has = any("atropos" in l.lower() and "dashboard" in l.lower()
                  for l in r.stdout.splitlines())
        return {"ok": True, "registered": has,
                "message": "registered" if has else "not registered"}


def serve(host="127.0.0.1", port=8787):
    # v18 B.3: on Railway, a deploy means new code — snapshot + backup first
    try:
        from . import railway
        res = railway.check_deploy()
        if res.get("changed"):
            history_log("railway", "deploy detected — snapshot+backup taken")
    except Exception:
        pass
    # Railway: bind to 0.0.0.0 and read $PORT (Railway requires this)
    try:
        from . import detect as _detect
        if _detect.detect_cloud() == "railway":
            host = "0.0.0.0"
            port = int(os.environ.get("PORT", port))
    except Exception:
        pass
    # kick off the periodic SSE status broadcaster (once, daemon thread)
    try:
        from .sse import start_status_broadcaster
        start_status_broadcaster()
    except Exception:
        pass
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Atropos dashboard on http://{host}:{port}")
    history_log("dashboard", "started")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cfg = config.load()
    d = cfg.get("dashboard", {})
    serve(d.get("host", "127.0.0.1"), int(d.get("port", 8787)))


def _activity_log(event: str, detail: str = ""):
    """Append to the activity timeline (24h feed) — never raises."""
    try:
        from . import activity
        activity.log(event, detail)
    except Exception:
        pass
