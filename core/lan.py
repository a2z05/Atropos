#!/usr/bin/env python3
"""Atropos LAN sharing — local address, share URL, ASCII QR frame, devices.

The authoritative share method is the URL: ``share_url()`` points at the
dashboard's LAN address so phones on the same network can open the
mobile chat page. ``qr_ascii()`` renders a *decorative* ASCII frame that
looks like a QR code (finder squares, border, caption). It is explicitly
NOT scannable — it exists so the terminal shows something friendly next
to the URL, never a fake promise that a camera can read it.

Devices ("pairing") persist in ``~/.atropos/devices.json``. Unknown
devices are auto-added as pending on their first touch; only approved
fingerprints pass ``is_approved()``. The list is capped at 50 entries —
oldest-pending evictions keep the file bounded.
"""
import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect

MAX_DEVICES = 50

_QR_BLOCK = "█"   # FULL BLOCK
_QR_GAP = " "          # light cell
_FINDER = 7            # finder square outer size in cells
_BORDER = 2            # quiet-zone cells around the frame
_CAPTION_PAD = 2       # spaces between frame and caption


# ── LAN address ───────────────────────────────────────────────────────────
def lan_ip() -> str:
    """Best-effort private IPv4 address of this machine.

    Uses the UDP-connect trick (no packets are actually sent): bind a UDP
    socket and ``connect`` to a public address, then read the source
    address the OS would use. Falls back to the hostname resolution, then
    ``127.0.0.1``. Never raises.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def share_url() -> str:
    """LAN URL of the dashboard: ``http://<lan_ip>:<port>/``."""
    port = config.get("dashboard.port") or 8787
    return f"http://{lan_ip()}:{port}/"


# ── decorative ASCII QR frame ─────────────────────────────────────────────
def qr_ascii(text: str, width: int = 21) -> list:
    """Render a decorative ASCII 'QR-style' frame around ``text``.

    The frame mimics a QR code's structure — three finder squares, a
    border and a quiet zone — but it is purely cosmetic. The cells carry
    no payload: a camera cannot scan this. Use ``share_url()`` as the
    authoritative share method. Returns a list of text lines.
    """
    text = (text or "").strip()
    cells = max(width, _FINDER * 2 + 5)
    # deterministic pseudo-random pattern derived from the text itself
    seed = sum(ord(c) for c in text) or 1
    module = 5
    pattern = []
    for i in range(cells):
        row = []
        for j in range(cells):
            row.append((seed * module) % 97 > 48)
            module = (module * 31 + 17) % 1000
        pattern.append(row)

    def cell(x: int, y: int) -> str:
        # finder squares: top-left, top-right, bottom-left
        for (fx, fy) in ((0, 0), (cells - _FINDER, 0), (0, cells - _FINDER)):
            if fx <= x < fx + _FINDER and fy <= y < fy + _FINDER:
                ox, oy = x - fx, y - fy
                ring = 0 < ox < _FINDER - 1 and 0 < oy < _FINDER - 1
                core = 1 < ox < _FINDER - 2 and 1 < oy < _FINDER - 2
                if core:
                    return _QR_BLOCK
                if ring:
                    return _QR_GAP
                return _QR_BLOCK
        return _QR_BLOCK if pattern[y][x] else _QR_GAP

    lines = []
    for y in range(cells):
        lines.append("".join(cell(x, y) for x in range(cells)))
    return _caption(lines, text)


def _caption(lines: list, text: str) -> list:
    """Return the frame lines with a right-hand caption (padded to fit)."""
    if not text:
        return lines
    width = len(lines[0])
    caption = [ln for ln in text.splitlines() if ln.strip()]
    out = []
    for i, ln in enumerate(lines):
        if i < len(caption):
            out.append(ln + " " * _CAPTION_PAD + caption[i])
        else:
            out.append(ln)
    return out


# ── device pairing ────────────────────────────────────────────────────────
def devices_path() -> Path:
    """Device list location: ``~/.atropos/devices.json``."""
    return detect.atropos_home() / "devices.json"


def _load_devices() -> list:
    try:
        raw = json.loads(devices_path().read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [d for d in raw if isinstance(d, dict)]
    except Exception:
        pass
    return []


def _save_devices(devices: list):
    p = devices_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(devices, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def touch(fingerprint: str, name: str = "") -> dict:
    """Register a device hit by fingerprint.

    An approved fingerprint just refreshes ``last_seen``. An unknown
    fingerprint is added as *pending* (``approved: False``) — the owner
    approves it via ``approve()`` before it counts as known. The list is
    capped at ``MAX_DEVICES`` (oldest entries evicted). Returns the
    device entry.
    """
    fingerprint = (fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("fingerprint is required")
    devices = _load_devices()
    now = _now_iso()
    for d in devices:
        if d.get("fingerprint") == fingerprint:
            d["last_seen"] = now
            if name:
                d["name"] = name[:64]
            _save_devices(devices)
            return dict(d)
    entry = {
        "id": _new_device_id(devices),
        "name": (name or "unknown")[:64],
        "fingerprint": fingerprint,
        "last_seen": now,
        "approved": False,
    }
    devices.append(entry)
    # cap the list: drop oldest entries, keep any approved ones
    pending = [d for d in devices if not d.get("approved")]
    overflow = len(devices) - MAX_DEVICES
    if overflow > 0:
        pending = pending[overflow:]
        approved = [d for d in devices if d.get("approved")]
        devices = pending + approved
    _save_devices(devices)
    return dict(entry)


def _new_device_id(devices: list) -> str:
    """Small unique hex id for a new device entry."""
    used = {d.get("id") for d in devices}
    import hashlib
    salt = str(time.time_ns()).encode()
    candidate = hashlib.sha1(salt).hexdigest()[:8]
    while candidate in used:
        salt += b"."
        candidate = hashlib.sha1(salt).hexdigest()[:8]
    return candidate


def is_approved(fingerprint: str) -> bool:
    """True when a fingerprint belongs to an approved device."""
    fingerprint = (fingerprint or "").strip()
    return any(
        d.get("fingerprint") == fingerprint and d.get("approved")
        for d in _load_devices()
    )


def pending_devices() -> list:
    """Devices awaiting approval (newest first)."""
    devices = [d for d in _load_devices() if not d.get("approved")]
    devices.sort(key=lambda d: d.get("last_seen", ""), reverse=True)
    return devices


def known_devices() -> list:
    """Approved devices (newest first)."""
    devices = [d for d in _load_devices() if d.get("approved")]
    devices.sort(key=lambda d: d.get("last_seen", ""), reverse=True)
    return devices


def approve(device_id: str) -> dict:
    """Approve a pending device. Returns the updated entry, or None."""
    devices = _load_devices()
    for d in devices:
        if d.get("id") == device_id:
            d["approved"] = True
            d["approved_at"] = _now_iso()
            _save_devices(devices)
            return dict(d)
    return None


def deny(device_id: str) -> bool:
    """Remove a pending device (or any entry). True when one was removed."""
    devices = _load_devices()
    kept = [d for d in devices if d.get("id") != device_id]
    if len(kept) == len(devices):
        return False
    _save_devices(kept)
    return True
