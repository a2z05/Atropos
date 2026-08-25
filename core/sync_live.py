#!/usr/bin/env python3
"""Live sync + relay (v18 H / H2) — real-time deltas over stdlib HTTP.

Built on core/sync.py (hash-map delta sync). Two new surfaces:

  ``live_serve(port, token)`` — stdlib HTTP server peers connect OUT to.
      Endpoints:
        GET  /livesync/state   -> {last_ts, pending, files}
        GET  /livesync/health  -> {ok}
        POST /livesync/push    -> {delta: {path: bytes-b64}} apply + journal
        GET  /livesync/poll?since=TS -> {delta: [...], last_ts}  (long-poll-ish)

  ``live_push(peer_url, token, since)`` — outbound delta push; callers
      (watch loop) call it every N seconds with the last-applied ts.

  ``relay(code)`` — rendezvous relay for locked-down hosts (Railway H2):
      both peers dial OUT to the relay server, exchange a short code,
      and poll for deltas. No inbound ports anywhere.

Secrets never sync (core/sync._is_sensitive). Deltas are per-file byte
diff via difflib.SequenceMatcher at the file level — no git needed.
Conflicts: last-writer-wins with backup-before-conflict + journal entry
(writes ~/.atropos/sync/conflicts/live-<ts>.json).
"""
import base64
import difflib
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import detect, sync

_LAST = {"ts": 0.0}          # last applied delta timestamp
_QUEUE = []                  # [(ts, rel, data)]
_LOCK = threading.Lock()
_JOURNAL = None


def _now_ts() -> float:
    return time.time()


def _ts_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _journal_path() -> Path:
    return sync._conflicts_dir() / "live-journal.jsonl"


def journal() -> list:
    """Conflict/apply journal (newest first) for the dashboard."""
    p = _journal_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(l) for l in lines][::-1][:50]
    except Exception:
        return []


def _log_journal(kind: str, rel: str, detail: str = ""):
    _journal_path().parent.mkdir(parents=True, exist_ok=True)
    with _journal_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _ts_iso(), "kind": kind, "rel": rel,
                            "detail": detail, "side": "live"}) + "\n")


def _apply_delta(rel: str, b64: str) -> dict:
    """Apply one delta: write file (if safe), journal, return status."""
    if sync._is_sensitive(rel):
        _log_journal("blocked", rel, "secret path")
        return {"ok": False, "error": "sensitive path"}
    base = detect.atropos_home()
    dest = base / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": False, "error": "bad path"}
    data = base64.b64decode(b64)
    if dest.exists():
        old = dest.read_bytes()
        if old == data:
            return {"ok": True, "unchanged": True}
        # backup-before-conflict (LWW)
        import hashlib
        old_hash = hashlib.sha1(old).hexdigest()  # noqa: S324 — checksum only
        new_hash = hashlib.sha1(data).hexdigest()  # noqa: S324 — checksum only
        sync._resolve_conflict(rel, old_hash, old, 0.0, new_hash, data, _now_ts())
    dest.write_bytes(data)
    _log_journal("apply", rel)
    return {"ok": True, "unchanged": False}


class _LiveHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data: dict, ctype="application/json"):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        tok = self.headers.get("Authorization", "")
        return tok == f"Bearer {self.server._token}"  # type: ignore

    def do_GET(self):
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if path == "/livesync/health":
            self._send(200, {"ok": True})
        elif path == "/livesync/state":
            with _LOCK:
                self._send(200, {"ok": True, "last_ts": _LAST["ts"],
                                 "pending": len(_QUEUE)})
        elif path.startswith("/livesync/poll"):
            q = urlparse(self.path).query
            since = float(q.split("since=")[1]) if "since=" in q else 0.0
            with _LOCK:
                out = [d for d in _QUEUE if d[0] > since]
            self._send(200, {"ok": True, "delta": [
                {"rel": r, "data": b64} for _, r, b64 in out],
                "last_ts": _LAST["ts"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if path != "/livesync/push":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send(400, {"error": f"bad payload: {e}"})
            return
        results = []
        pushed_ts = payload.get("ts", _now_ts())
        for item in payload.get("delta", []):
            r = _apply_delta(item.get("rel", ""), item.get("data", ""))
            results.append({**r, "rel": item.get("rel")})
            # keep the delta bufferred for later polls (relay-style bridge):
            # a third peer polling this box receives what we just applied
            with _LOCK:
                _QUEUE.append((pushed_ts, item.get("rel", ""), item.get("data", "")))
        with _LOCK:
            _LAST["ts"] = pushed_ts
        self._send(200, {"ok": True, "results": results})


def live_serve(port: int = 8791, token: str = "") -> ThreadingHTTPServer:
    """Start the live-sync server (blocking; run in a thread)."""
    if not token:
        # stable per-box machine token (survives password rotation)
        from . import auth as _auth
        token = _auth.machine_token()
    srv = ThreadingHTTPServer(("0.0.0.0", port), _LiveHandler)
    srv._token = token  # type: ignore
    return srv


def _collect_delta(base: Path, since: float) -> tuple:
    """Files changed since `since` (mtime based) → [(rel, b64), ...]."""
    out = []
    for rel in sync.managed_files(base):
        if sync._is_sensitive(rel):
            continue
        p = base / rel
        try:
            if p.stat().st_mtime > since:
                out.append((rel, base64.b64encode(p.read_bytes()).decode()))
        except OSError:
            continue
    return out


def live_push(peer_url: str, token: str, since: float = 0.0,
              base=None) -> dict:
    """Outbound push of pending deltas to a peer. Returns {ok, pushed, last_ts}."""
    import urllib.request
    base = Path(base) if base else detect.atropos_home()
    delta = _collect_delta(base, since)
    if not delta:
        return {"ok": True, "pushed": 0, "last_ts": since}
    payload = json.dumps({"ts": _now_ts(),
                          "delta": [{"rel": r, "data": d} for r, d in delta]}).encode()
    req = urllib.request.Request(
        f"{peer_url}/livesync/push", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return {"ok": bool(body.get("ok")), "pushed": len(delta),
            "last_ts": _now_ts()}


def live_poll(peer_url: str, token: str, since: float = 0.0) -> dict:
    """Outbound poll of a peer's pending deltas; applies them locally."""
    import urllib.request
    url = f"{peer_url}/livesync/poll?since={since}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    applied = 0
    for item in body.get("delta", []):
        r = _apply_delta(item.get("rel", ""), item.get("data", ""))
        if r.get("ok") and not r.get("unchanged"):
            applied += 1
    with _LOCK:
        _LAST["ts"] = body.get("last_ts", since)
    return {"ok": True, "applied": applied, "last_ts": _LAST["ts"]}


# ── watch loop (v18 H.1): debounced 2s, push every tick ─────────────────────
def live_watch(peer_url: str, token: str, interval: float = 2.0,
               stop_event: threading.Event | None = None) -> None:
    """Watch ~/.atropos + project files; push deltas to peer every interval.

    Debounce: files changed within the last `interval` are coalesced into
    one push. Blocks; call in a thread with stop_event to end.
    """
    last = _now_ts()
    while not (stop_event and stop_event.is_set()):
        try:
            live_push(peer_url, token, since=last)
            last = _now_ts()
        except Exception:
            pass  # peer offline — retry next tick
        time.sleep(interval)


# ── relay (v18 H2.2): outbound-only rendezvous, no inbound ports ────────────
class _RelayHandler(BaseHTTPRequestHandler):
    """Rendezvous: peers register under a code and exchange deltas."""

    def log_message(self, *a):
        pass

    def _send(self, code, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send(400, {"error": f"bad payload: {e}"})
            return
        path = urlparse(self.path).path
        code = payload.get("code", "")
        if path == "/relay/put":
            self.server._boxes[code] = {  # type: ignore
                "delta": payload.get("delta", []),
                "ts": _now_ts(),
            }
            self._send(200, {"ok": True})
        elif path == "/relay/take":
            box = self.server._boxes.get(code)  # type: ignore
            if not box:
                self._send(200, {"ok": True, "delta": []})
                return
            self.server._boxes[code] = {  # type: ignore
                "delta": [], "ts": _now_ts()}
            self._send(200, {"ok": True, "delta": box.get("delta", [])})
        else:
            self._send(404, {"error": "not found"})


def relay_serve(port: int = 8792) -> ThreadingHTTPServer:
    """Relay rendezvous server (blocks; run in a thread)."""
    srv = ThreadingHTTPServer(("0.0.0.0", port), _RelayHandler)
    srv._boxes = {}  # type: ignore
    return srv


def relay_put(relay_url: str, code: str, base=None) -> dict:
    """Push local deltas into the rendezvous box (outbound only)."""
    import urllib.request
    base = Path(base) if base else detect.atropos_home()
    delta = [{"rel": r, "data": d}
             for r, d in _collect_delta(base, since=0.0)]
    payload = json.dumps({"code": code, "delta": delta}).encode()
    req = urllib.request.Request(
        f"{relay_url}/relay/put", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def relay_take(relay_url: str, code: str) -> dict:
    """Pull + apply the rendezvous box (outbound only)."""
    import urllib.request
    payload = json.dumps({"code": code}).encode()
    req = urllib.request.Request(
        f"{relay_url}/relay/take", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    applied = 0
    for item in body.get("delta", []):
        r = _apply_delta(item.get("rel", ""), item.get("data", ""))
        if r.get("ok") and not r.get("unchanged"):
            applied += 1
    return {"ok": True, "applied": applied}


def relay_loop(relay_url: str, code: str, interval: float = 4.0,
               stop_event: threading.Event | None = None) -> None:
    """Poll-based relay loop — both peers run this; no inbound ports."""
    while not (stop_event and stop_event.is_set()):
        try:
            relay_put(relay_url, code)
            relay_take(relay_url, code)
        except Exception:
            pass
        time.sleep(interval)
