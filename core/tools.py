#!/usr/bin/env python3
"""Atropos tools — Hermes/Claude feature parity, stdlib only.

Each tool is a thin, safe wrapper: it checks for its backend (CLI binary,
API key, local service) and returns a clear message when absent. Never
fabricates results. Commands: search, cron, web, kanban, email, tts,
vision, imagine, video, youtube, x, docs, hue, audio, delegate, bridge.
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from . import detect, settings


def _have(cmd: str) -> bool:
    try:
        subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
        return True
    except Exception:
        return False


def _run(cmd: list, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": proc.returncode == 0, "output": proc.stdout.strip(),
                "error": proc.stderr.strip() or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── search (FTS across chat sessions) ────────────────────────────────────
def search(query: str, k: int = 10) -> dict:
    from . import chat
    return {"ok": True, "query": query, "results": chat.search_messages(query, k)}


# ── cron mgmt ────────────────────────────────────────────────────────────
def cron_list() -> dict:
    """List cron jobs: Hermes JSON store first, legacy yaml sidecar second."""
    from . import cron as _cron
    try:
        jobs = _cron.list_jobs()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    legacy = _cron._yaml_jobs() if hasattr(_cron, "_yaml_jobs") else []
    for j in legacy:
        if j.get("job_id") not in {x.get("id") for x in jobs}:
            jobs.append({
                "file": f"{j.get('job_id')}.yaml", "schedule": j.get("schedule"),
                "command": j.get("command"), "enabled": j.get("enabled", True),
            })
    return {"ok": True, "jobs": jobs}


# ── web search / fetch (9Router /v1/search + /v1/web/fetch) ──────────────
def _gateway_ready() -> bool:
    """Return True when the 9Router gateway env vars are both set."""
    return bool(
        os.environ.get("NINEROUTER_URL", "").rstrip("/")
        and os.environ.get("NINEROUTER_KEY")
    )


def web_search(query: str, k: int = 5) -> dict:
    """Web search: 9Router gateway first, Hermes-style providers as fallback.

    The gateway is the first provider (unchanged behavior); when it is
    unconfigured or errors, core.web.web_search (ported from
    hermes-agent/tools/web_tools.py) takes over with its own backend chain
    and the same {ok, results} shape.
    """
    from . import web as _web
    url = os.environ.get("NINEROUTER_URL", "").rstrip("/") + "/v1/search"
    key = os.environ.get("NINEROUTER_KEY", "")
    if _gateway_ready():
        try:
            req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return {"ok": True, "results": json.loads(r.read().decode("utf-8"))}
        except Exception:
            pass  # gateway error -> hermes provider chain below
    return _web.web_search(query, k=k)


def web_fetch(target: str) -> dict:
    """Web fetch: 9Router gateway first, Hermes-style extraction as fallback.

    Fallback (core.web.web_fetch, ported from web_tools.py) returns the
    clean page text in the same {ok, content} shape, including SSRF and
    embedded-secret URL checks.
    """
    from . import web as _web
    url = os.environ.get("NINEROUTER_URL", "").rstrip("/") + "/v1/web/fetch"
    key = os.environ.get("NINEROUTER_KEY", "")
    if _gateway_ready():
        try:
            req = urllib.request.Request(url, data=json.dumps({"url": target}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return {"ok": True, "content": json.loads(r.read().decode("utf-8"))}
        except Exception:
            pass  # gateway error -> hermes fetch chain below
    return _web.web_fetch(target)


# ── kanban (JSON board in ~/.atropos/kanban.json) ────────────────────────
def _kanban_path() -> Path:
    return detect.atropos_home() / "kanban.json"


def kanban_list() -> dict:
    p = _kanban_path()
    cols = {"todo": [], "doing": [], "done": []}
    if p.exists():
        try:
            cols.update(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return {"ok": True, "cols": cols}


def kanban_add(text: str, col: str = "todo") -> dict:
    cols = kanban_list()["cols"]
    col = col if col in cols else "todo"
    cols[col].append({"text": text, "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())})
    _kanban_path().parent.mkdir(parents=True, exist_ok=True)
    _kanban_path().write_text(json.dumps(cols, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "cols": cols}


def kanban_move(text: str, col: str) -> dict:
    cols = kanban_list()["cols"]
    for k in cols:
        cols[k] = [c for c in cols[k] if c["text"] != text]
    cols = kanban_list()["cols"]
    for src in cols:
        for i, c in enumerate(cols[src]):
            if c["text"] == text:
                cols[src].pop(i)
                break
    cols[col if col in cols else "todo"].append({"text": text, "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())})
    _kanban_path().parent.mkdir(parents=True, exist_ok=True)
    _kanban_path().write_text(json.dumps(cols, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "cols": cols}


# ── email (himalaya IMAP/SMTP) ───────────────────────────────────────────
def email_inbox(n: int = 5) -> dict:
    if not _have("himalaya"):
        return {"ok": False, "error": "himalaya CLI not found"}
    return _run(["himalaya", "envelope", "list", "-s", str(n)])


def email_send(to: str, subject: str, body: str) -> dict:
    if not _have("himalaya"):
        return {"ok": False, "error": "himalaya CLI not found"}
    return _run(["himalaya", "message", "send"], timeout=90) or {"ok": False, "error": "himalaya TUI mode — run interactively"}


# ── TTS / vision / image / video (9Router gateway) ───────────────────────
def _gateway(kind: str, payload: dict) -> dict:
    url = os.environ.get("NINEROUTER_URL", "").rstrip("/") + "/v1/" + kind
    key = os.environ.get("NINEROUTER_KEY", "")
    if not url or not key:
        return {"ok": False, "error": "NINEROUTER_URL/NINEROUTER_KEY not set"}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tts(text: str, voice: str = "") -> dict:
    return _gateway("tts", {"text": text, "voice": voice})


def vision(image_path: str, prompt: str = "Describe this image") -> dict:
    return _gateway("vision", {"image_path": image_path, "prompt": prompt})


def imagine(prompt: str) -> dict:
    return _gateway("images/generations", {"prompt": prompt})


def video(prompt: str) -> dict:
    return _gateway("videos/generations", {"prompt": prompt})


def youtube(url: str) -> dict:
    if not _have("yt-dlp"):
        return {"ok": False, "error": "yt-dlp not found"}
    return _run(["yt-dlp", "--skip-download", "--write-auto-sub", "--sub-format", "txt",
                 "--write-info-json", "-o", "%(id)s.%(ext)s", url], timeout=180)


# ── X / Twitter ──────────────────────────────────────────────────────────
def x_post(text: str) -> dict:
    """Post a tweet via core/x.py (xurl CLI wrapper, ported from the xurl
    skill). Return shape unchanged: {ok, output/error}."""
    from . import x as _x
    return _x.x_post(text)


# ── office docs (python-docx etc unavailable — dry-run manifest) ─────────
def docs(kind: str, path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    return {"ok": True, "kind": kind, "path": str(p), "size": p.stat().st_size,
            "note": "view only — editing needs the hermes productivity stack"}


# ── smart home (openhue) ─────────────────────────────────────────────────
def hue(command: str) -> dict:
    if not _have("openhue"):
        return {"ok": False, "error": "openhue not found"}
    return _run(["openhue", command] if command else ["openhue"])


# ── audio info ───────────────────────────────────────────────────────────
def audio_info(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    return {"ok": True, "path": str(p), "size": p.stat().st_size}


# ── delegation (subagent via agents) ─────────────────────────────────────
def delegate(goal: str, agent: str | None = None) -> dict:
    from . import agents
    if agent:
        return agents.run_agent(agent, goal)
    name = "delegate-" + re.sub(r"\W+", "-", goal[:20]).strip("-").lower() or "delegate"
    try:
        agents.save_agent({"name": name, "description": "spawned delegation",
                           "prompt": goal, "harness": "auto", "model": None,
                           "effort": "medium", "tools": ["*"], "permissions": "default"})
        return agents.run_agent(name, goal)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── bridge (RAFT-style wake/activity endpoint) ───────────────────────────
_BRIDGE_STATE = {"started": None, "events": []}


def bridge_start(port: int = 8765) -> dict:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    token = settings.get("bridge.token", "") or ""
    if not token:
        token = os.urandom(16).hex()
        settings.set("bridge.token", token)

    class Handler(BaseHTTPRequestHandler):
        def _auth(self):
            return self.headers.get("X-Atropos-Token") == token

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"ok": True, "bridge": "atropos"})
            elif self.path == "/activity" and self._auth():
                self._json(200, {"events": _BRIDGE_STATE["events"][-100:]})
            else:
                self._json(401, {"error": "unauthorized"})

        def do_POST(self):
            if not self._auth():
                self._json(401, {"error": "unauthorized"})
                return
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except ValueError:
                body = {}
            if self.path == "/wake":
                _BRIDGE_STATE["events"].append({"event": "wake", "body": body})
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

        def _json(self, code, data):
            b = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _BRIDGE_STATE["started"] = port
    return {"ok": True, "port": port, "token": token,
            "wake": f"POST http://127.0.0.1:{port}/wake (X-Atropos-Token header)"}


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1:2] or ["help"]
    print(json.dumps({"tool": cmd[0], "hint": "use via `atropos <tool> …`"}, indent=2))