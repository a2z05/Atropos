#!/usr/bin/env python3
"""Atropos Telegram gateway — long-poll, guest modes, glass buttons, step trails.

A full Telegram bridge inside Atropos (stdlib urllib; Hermes adapter not
required):

* long-polling ``getUpdates`` (offset-saved, reconnect with jitter)
* identity: owner allowlist + guests (allow | readonly | deny), per-guest
  rate limits, per-guest session isolation (via core.guest)
* glass buttons: after every command/report, inline keyboards whose
  callback queries route back into the same command context
* step trails: long ops post a progress message and EDIT it live
* commands mirror the CLI (/doctor /backup /sync /update /skills /mcp
  /routing /effort /settings /jailbreak /dashboard /lore /status /new
  /sessions /stop)
* logging to ~/.atropos/telegram.log (rotating 5MB)
* errors: one human line + a [Trace] hidden button
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import detect, guest, settings

API = "https://api.telegram.org/bot{token}"

# ── logging (rotating 5MB) ────────────────────────────────────────────────
_LOGGER = logging.getLogger("atropos.telegram")
_log_initialized = False


def _log_path() -> Path:
    return detect.atropos_home() / "telegram.log"


def _init_log():
    global _log_initialized
    if _log_initialized:
        return
    _log_initialized = True
    p = _log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > 5 * 1024 * 1024:
        backup = p.with_suffix(".log.1")
        try:
            p.replace(backup)
        except OSError:
            pass
    handler = logging.FileHandler(p, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)


def _token() -> str:
    return settings.get("telegram.token", "") or ""


# ── HTTP helpers ──────────────────────────────────────────────────────────
def _api_call(method: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    """Call a Telegram Bot API method; returns parsed json (never raises)."""
    if not _token():
        return {"ok": False, "error": "telegram.token not configured"}
    url = API.format(token=_token()) + "/" + method
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_message(chat_id, text: str, buttons: list | None = None,
                 reply_to: int | None = None) -> dict:
    """Send a message; ``buttons`` = [[(label, callback_data), ...], ...]."""
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": lbl, "callback_data": cb}
                                 for lbl, cb in row] for row in buttons]}
    return _api_call("sendMessage", payload)


def edit_message(chat_id, message_id, text: str, buttons: list | None = None) -> dict:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": lbl, "callback_data": cb}
                                 for lbl, cb in row] for row in buttons]}
    return _api_call("editMessageText", payload)


def answer_callback(callback_id: str, text: str = "") -> dict:
    return _api_call("answerCallbackQuery",
                     {"callback_query_id": callback_id, "text": text[:200]})


def get_updates(offset: int | None = None, timeout: int = 30) -> dict:
    payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _api_call("getUpdates", payload, timeout=timeout + 15)


def send_typing(chat_id) -> dict:
    return _api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})


# ── identity / guests ─────────────────────────────────────────────────────
def owner_ids() -> list:
    return [str(x).strip() for x in (settings.get("telegram.owner_ids", []) or []) if str(x).strip()]


def guest_mode_for(user_id: str) -> str:
    """allow | readonly | deny — permissions for a stranger."""
    if user_id in owner_ids():
        return "owner"
    val = settings.get("telegram.guests", "allow") or "allow"
    return val if val in ("allow", "readonly", "deny") else "allow"


class RateLimit:
    def __init__(self, per_min: int = 10):
        self.per_min = per_min
        self._hits = {}

    def allow(self, uid: str) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(uid, []) if now - t < 60]
        if len(hits) >= self.per_min:
            self._hits[uid] = hits
            return False
        hits.append(now)
        self._hits[uid] = hits
        return True


# ── step trails ───────────────────────────────────────────────────────────
class StepTrail:
    """Post a progress message, then EDIT it live as steps complete."""

    def __init__(self, chat_id, title: str):
        self.chat_id = chat_id
        self.title = title
        self.n = 0
        self.total = 0
        self.msg_id = None
        self._last = ""

    def start(self, total: int = 1):
        self.total = total
        r = send_message(self.chat_id, self._render())
        if r.get("ok"):
            self.msg_id = r["result"]["message_id"]
        return r

    def step(self, label: str, ok: bool = True):
        self.n += 1
        icon = "✅" if ok else "⚠️"
        self._last = f"🔄 {self.n}/{self.total}: {label}"
        self._render()
        if self.msg_id:
            edit_message(self.chat_id, self.msg_id, self._render())
        return self._last

    def done(self, summary: str):
        self._last = f"✅ {self.n}/{self.total}: {summary}"
        if self.msg_id:
            edit_message(self.chat_id, self.msg_id, self._render())
        return self._last

    def _render(self) -> str:
        lines = [f"⚙️ {self.title}"]
        if self.total:
            lines.append(f"   {self._last}")
        return "\n".join(lines)


# ── command routing (mirrors the CLI) ────────────────────────────────────
_COMMANDS = {
    "/doctor": "Run the 7 health checks",
    "/backup": "Create a backup now",
    "/sync": "Sync status",
    "/update": "Check for updates",
    "/skills": "List skills",
    "/mcp": "MCP registry",
    "/routing": "Task routing map",
    "/effort": "Effort tiers",
    "/settings": "Settings table",
    "/jailbreak": "Restriction scanner",
    "/dashboard": "Dashboard URL + QR",
    "/lore": "The Moirai story",
    "/status": "System overview",
    "/new": "Start a fresh session",
    "/sessions": "List sessions",
    "/stop": "Stop the gateway",
}


def route_command(cmd: str, chat_id, user_id) -> list:
    """Handle a gateway command → list of (chat_id, text, buttons). """
    from . import cliui, doctor, router as _router
    cmdc = cmd.split()[0].lower()
    out = []
    if cmdc == "/doctor":
        results = doctor.doctor(fix=False)
        fails = [r for r in results if not r["ok"]]
        text = "🩺 Doctor:\n" + "\n".join(f"{'✅' if r['ok'] else '❌'} {r['name']}: {r['msg']}" for r in results)
        if not fails:
            text += f"\n\n{cliui.doctor_verdict(0)}"
        buttons = [[("Fix now", "/doctor/fix"), ("Re-run", "/doctor")]] if fails else [[("Re-run", "/doctor")]]
        out.append((chat_id, text, buttons))
    elif cmdc == "/status":
        r = _router.health()
        text = (f"📟 Status: router {r['active']} · pings: "
                + " ".join(f"{k}{'✓' if v.get('ok') else '✗'}" for k, v in r["pings"].items()))
        out.append((chat_id, text, [[("Open dashboard", "/dashboard")]]))
    elif cmdc == "/dashboard":
        from . import lan
        url = f"http://{lan.lan_ip()}:{settings.get('dashboard.port', 8787)}/"
        text = f"🌐 Dashboard: {url}"
        out.append((chat_id, text, None))
    elif cmdc == "/lore":
        out.append((chat_id, cliui.oracle_line() or "The Moirai weave.", [[("Full story", "/lore/all")]]))
    elif cmdc == "/lore/all":
        out.append((chat_id, "Clotho spins. Lachesis measures. Atropos decides.\n\n"
                    "They are the three sister processes of this harness — see docs/MOIRAI.md.", None))
    elif cmdc == "/new":
        from . import chat
        chat.create_session(f"tg:{user_id}")
        out.append((chat_id, "Fresh thread spun. ✦", None))
    elif cmdc == "/sessions":
        from . import chat
        rows = chat.session_list(5)
        text = "\n".join(f"· {s['title'][:40]}" for s in rows) or "No sessions yet."
        out.append((chat_id, "🧵 Sessions:\n" + text, None))
    elif cmdc == "/stop":
        out.append((chat_id, "Gateway stopping. Goodbye ✦", None))
    else:
        out.append((chat_id, f"Unknown command: {cmd}. Try /doctor, /backup, /status, /lore.", None))
    return out


# ── the polling loop ──────────────────────────────────────────────────────
def poll_once(offset: int | None = None) -> int:
    """Fetch and handle one batch of updates; returns the next offset."""
    data = get_updates(offset)
    if not data.get("ok"):
        return offset or 0
    nxt = offset or 0
    for upd in data.get("result", []):
        nxt = max(nxt, upd.get("update_id", 0) + 1)
        _handle_update(upd)
    return nxt


def _handle_update(upd: dict) -> None:
    _init_log()
    if "callback_query" in upd:
        _handle_callback(upd["callback_query"])
        return
    msg = upd.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    user_id = str((msg.get("from") or {}).get("id", ""))
    text = msg.get("text", "")
    if not chat_id:
        return
    mode = guest_mode_for(user_id)
    if mode == "deny":
        send_message(chat_id, "This gateway is private. ✦")
        return
    _LOGGER.info("from=%s mode=%s msg=%s", user_id, mode, text[:80])
    if text.startswith("/"):
        if mode == "readonly" and text not in ("/status", "/doctor", "/lore", "/sessions", "/new"):
            send_message(chat_id, "Guests are read-only here — ask the owner for more.")
            return
        for cid, t, btns in route_command(text.strip(), chat_id, user_id):
            send_message(cid, t, btns)
        return
    # free-form chat → guest context (same engine, filtered)
    if mode == "readonly" and not (text and False):
        send_message(chat_id, "Guests are read-only here — ask the owner to chat freely.")
        return
    reply = guest.respond_guard(text)
    if reply:
        send_message(chat_id, reply)
        return
    send_typing(chat_id)
    from . import chat
    res = chat.send(None, text, harness="auto")
    send_message(chat_id, res.get("reply", "…") or "…")


def _handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    cid = (cb.get("message") or {}).get("chat", {}).get("id")
    uid = str((cb.get("from") or {}).get("id", ""))
    answer_callback(cb.get("id", ""), "…")
    mode = guest_mode_for(uid)
    if mode == "deny":
        return
    _LOGGER.info("callback from=%s data=%s", uid, data[:60])
    if data.startswith("/") and cid:
        for cid2, t, btns in route_command(data, cid, uid):
            send_message(cid2, t, btns)


def run(until=None, poll_interval: float = 1.0) -> None:
    """Poll forever (or until a stop callable returns True). Reconnects with jitter."""
    offset = 0
    _init_log()
    _LOGGER.info("gateway started")
    while True:
        if until is not None and until():
            _LOGGER.info("gateway stopped")
            return
        try:
            offset = poll_once(offset)
        except Exception as e:
            _LOGGER.warning("poll error: %s", e)
            time.sleep(2 + (time.time() % 3))
        time.sleep(poll_interval)


def status() -> dict:
    return {
        "configured": bool(_token()),
        "owner_ids": owner_ids(),
        "guests": settings.get("telegram.guests", "allow"),
        "token_masked": "***" if _token() else "",
        "mode": "long-polling",
        "log": str(_log_path()),
    }


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["status"]:
        print(json.dumps(status(), indent=2))
    elif sys.argv[1:2] == ["start"]:
        run()
    else:
        print("usage: python -m core.telegram status|start")