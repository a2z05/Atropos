#!/usr/bin/env python3
"""Atropos SSE hub — live push for the dashboard.

One ``EventSource`` per dashboard tab on ``/api/events`` feeds every panel
through named channels (status, logs, console, notify). Bounded queues
per client (drop-oldest), a hard client cap, and heartbeat keepalive so
half-open TCP connections get evicted instead of leaking threads.

HTTP surface lives in core/dashboard.py; this module is the fan-out
engine only. Thread-safe: broadcast() may be called from any worker.
"""
import json
import queue
import threading
import time

MAX_CLIENTS = 32
MAX_QUEUE = 200
HEARTBEAT_SECONDS = 15


class Hub:
    """In-memory pub/sub with per-client bounded queues."""

    def __init__(self):
        self._clients = {}
        self._lock = threading.Lock()

    # ── client lifecycle ──────────────────────────────────────────────
    def subscribe(self, client_id: str, q: queue.Queue):
        """Register a client. Evicts oldest when at capacity."""
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                # evict the oldest subscriber to make room
                oldest = min(self._clients, key=lambda k: self._clients[k][1])
                self._clients.pop(oldest, None)
            self._clients[client_id] = (q, time.monotonic())

    def unsubscribe(self, client_id: str):
        """Drop a client (also called after failed writes)."""
        with self._lock:
            self._clients.pop(client_id, None)

    def touch(self, client_id: str):
        """Refresh a client's heartbeat timestamp."""
        with self._lock:
            entry = self._clients.get(client_id)
            if entry:
                self._clients[client_id] = (entry[0], time.monotonic())

    def is_subscribed(self, client_id: str) -> bool:
        """True when the client is still registered."""
        with self._lock:
            return client_id in self._clients

    # ── fan-out ───────────────────────────────────────────────────────
    def broadcast(self, channel: str, data: dict):
        """Push one frame to every subscriber of a channel."""
        frame = json.dumps({"channel": channel, "data": data},
                           ensure_ascii=False).encode("utf-8")
        with self._lock:
            clients = list(self._clients.items())
        for cid, (q, _last) in clients:
            try:
                q.put_nowait(frame)
            except queue.Full:
                # drop-oldest then retry once
                try:
                    q.get_nowait()
                    q.put_nowait(frame)
                except Exception:
                    pass

    def active_count(self) -> int:
        """Number of live subscribers."""
        with self._lock:
            return len(self._clients)


hub = Hub()  # module-level singleton


# ── periodic status broadcaster ──────────────────────────────────────────
def _status_broadcaster(interval: float = 10.0):
    """Daemon thread: push a compact status payload to every subscriber.

    Started lazily by start_status_broadcaster(); exits with the process.
    """
    while True:
        time.sleep(interval)
        try:
            from . import settings as _settings
            interval = _settings.get("dashboard.refresh_ms", 10000) / 1000.0
            from . import detect as _detect, router as _router
            payload = {
                "router": _router.get().get("active", "?"),
                "model": _router.get().get("model", ""),
                "uptime": int(time.monotonic()),
            }
            hub.broadcast("status", payload)
        except Exception:
            pass


_broadcaster_started = False
_broadcaster_lock = threading.Lock()


def start_status_broadcaster():
    """Start the periodic status broadcaster once (process-wide)."""
    global _broadcaster_started
    with _broadcaster_lock:
        if _broadcaster_started:
            return
        _broadcaster_started = True
        threading.Thread(target=_status_broadcaster, daemon=True).start()


# ── generator for the HTTP handler ────────────────────────────────────────
def stream(client_id: str, timeout: float = 35.0):
    """Yield SSE frames for one client until it stalls or disconnects.

    Yields ``str`` frames (already encoded "data: ..."). Heartbeat every
    HEARTBEAT_SECONDS; if the client hasn't consumed for ``timeout``
    seconds it is unsubscribed and the stream ends.
    """
    q = queue.Queue(maxsize=MAX_QUEUE)
    hub.subscribe(client_id, q)
    last_pulse = time.monotonic()
    try:
        while True:
            try:
                frame = q.get(timeout=HEARTBEAT_SECONDS)
                yield b"data: " + frame + b"\n\n"
                hub.touch(client_id)
                last_pulse = time.monotonic()
                # also refresh the pulse deadline after real frames
            except queue.Empty:
                if time.monotonic() - last_pulse > timeout:
                    break
                yield b": ping\n\n"
    finally:
        hub.unsubscribe(client_id)