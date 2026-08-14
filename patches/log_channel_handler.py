"""Log channel hook: forward inbound events to private channel + manual send.

Events handled:
  agent:start  -> log who/where/how/what of every inbound message
  command:*    -> log every slash command (incl. /start bot starts)
  agent:end    -> log response summary (model, provider)

Channel commands (from the log channel only):
  /users                 -> list everyone who contacted the bot (id, name, first seen)
  /send <id> <msg...>    -> send a manual message to that user via the bot
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_CHANNEL = -1003744718087  # "ATRA log" supergroup

ENV_PATH = "/data/.hermes/.env"
USERS_FILE = os.path.join(HOOK_DIR, "known_users.json")
LOG_FILE = os.path.join(HOOK_DIR, "channel_log.jsonl")


def _load_env() -> dict:
    env = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        logger.error("log_channel: env load failed: %s", e)
    return env


def _token() -> str | None:
    return _load_env().get("TELEGRAM_BOT_TOKEN")


async def _tg(method: str, **params):
    """Call Telegram Bot API. Returns parsed json or None."""
    token = _token()
    if not token:
        logger.error("log_channel: no TELEGRAM_BOT_TOKEN")
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=params)
            if r.status_code != 200:
                logger.error("log_channel: tg %s -> %s %s", method, r.status_code, r.text[:200])
                return None
            return r.json()
    except Exception as e:
        logger.error("log_channel: tg %s failed: %s", method, str(e)[:100])
        return None


def _load_users() -> dict:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(users: dict):
    try:
        with open(USERS_FILE, "w") as f:
            json.dump(users, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.error("log_channel: save users failed: %s", e)


def _append_log(entry: dict):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("log_channel: append log failed: %s", e)


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _record_user(user_id, username, first_name, chat_id, chat_type):
    """Remember anyone who contacts the bot."""
    users = _load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "first_seen": _ts(),
            "id": user_id,
            "username": username or "",
            "first_name": first_name or "",
        }
    else:
        users[uid]["username"] = username or users[uid].get("username", "")
        users[uid]["first_name"] = first_name or users[uid].get("first_name", "")
    users[uid]["last_seen"] = _ts()
    users[uid]["last_chat"] = chat_id
    users[uid]["last_chat_type"] = chat_type
    _save_users(users)


def _fmt_user(u: dict) -> str:
    parts = []
    if u.get("first_name"):
        parts.append(u["first_name"])
    if u.get("username"):
        parts.append(f"@{u['username']}")
    if not parts:
        parts.append(str(u.get("id", "?")))
    return " ".join(parts)


def _fmt_event(event_type: str, ctx: dict) -> str:
    uid = ctx.get("user_id", "?")
    platform = ctx.get("platform", "?")
    chat_id = ctx.get("chat_id", "?")
    chat_type = ctx.get("chat_type", "")
    msg = ctx.get("message", "") or ""
    msg = msg[:400]

    if event_type == "agent:start":
        head = "📩 پیام ورودی"
    elif event_type == "command:*" or event_type.startswith("command:"):
        head = "⚙️ دستور"
    elif event_type == "agent:end":
        head = "✅ پاسخ ارسال شد"
    elif event_type == "session:start":
        head = "🆕 سشن جدید"
    else:
        head = f"🔔 {event_type}"

    lines = [
        f"{head} — {_ts()}",
        f"👤 کاربر: {uid}",
        f"📍 {platform} | چت: {chat_id} ({chat_type})",
    ]
    if msg:
        lines.append(f"💬 {msg}")
    if event_type == "agent:end":
        lines.append(f"🤖 {ctx.get('model', '?')} / {ctx.get('provider', '?')}")
    return "\n".join(lines)


async def _reply_in_channel(text: str):
    await _tg("sendMessage", chat_id=LOG_CHANNEL, text=text, disable_web_page_preview=True)


async def _handle_channel_command(text: str, sender_id: int):
    """Handle !commands coming FROM the log channel."""
    if sender_id != 5838175445:  # only Artan
        await _reply_in_channel("این دستور فقط برای مالک کاناله.")
        return

    text = (text or "").strip()

    # !help
    if text in ("!help", "!help ", "!راهنما"):
        await _reply_in_channel(
            "📋 دستورهای کانال:\n"
            "!users — لیست همه کاربرا\n"
            "!users @username — جستجوی کاربر\n"
            "!who <id> — جزئیات یک کاربر\n"
            "!send <id> <متن> — ارسال پیام به کاربر\n"
            "!sendall <متن> — ارسال به همه\n"
            "!stats — آمار پیامها"
        )
        return

    # !users / !users <filter>
    if text.startswith("!users"):
        users = _load_users()
        if not users:
            await _reply_in_channel("هنوز کسی پیام نداده.")
            return
        q = text[len("!users"):].strip()
        lines = [f"👥 {len(users)} کاربر:"]
        for uid, u in list(users.items()):
            name = _fmt_user(u)
            if q and q.lower() not in (uid + " " + name).lower():
                continue
            lines.append(f"• {name} — id:{uid} (اولین: {u.get('first_seen', '?')})")
        if len(lines) == 1:
            await _reply_in_channel("چیزی پیدا نشد.")
            return
        await _reply_in_channel("\n".join(lines))
        return

    # !who <id>
    m = re.match(r"^!who\s+(\d+)$", text)
    if m:
        uid = m.group(1)
        users = _load_users()
        u = users.get(uid)
        if not u:
            await _reply_in_channel(f"کاربر {uid} پیدا نشد.")
            return
        await _reply_in_channel(
            f"👤 {_fmt_user(u)}\n"
            f"🆔 id: {uid}\n"
            f"📅 اولین: {u.get('first_seen', '?')}\n"
            f"🕐 آخرین: {u.get('last_seen', '?')}\n"
            f"💬 آخرین چت: {u.get('last_chat', '?')} ({u.get('last_chat_type', '?')})"
        )
        return

    # !send <id> <msg>
    m = re.match(r"^!send\s+(\d+)\s+(.+)$", text, re.S)
    if m:
        target = int(m.group(1))
        body = m.group(2).strip()
        res = await _tg(
            "sendMessage",
            chat_id=target,
            text=body,
            disable_web_page_preview=True,
        )
        if res and res.get("ok"):
            await _reply_in_channel(f"✅ ارسال شد به {target}:\n{body[:200]}")
        else:
            await _reply_in_channel(f"❌ ارسال به {target} نشد — اون بات رو استارت نکرده یا بلاک کرده.")
        return

    # !sendall <msg> — send to every known user except owner
    m = re.match(r"^!sendall\s+(.+)$", text, re.S)
    if m:
        body = m.group(1).strip()
        users = _load_users()
        targets = [uid for uid in users if uid != "5838175445"]
        if not targets:
            await _reply_in_channel("کسی توی لیست نیست.")
            return
        ok = 0
        failed = []
        for uid in targets:
            res = await _tg("sendMessage", chat_id=int(uid), text=body, disable_web_page_preview=True)
            if res and res.get("ok"):
                ok += 1
            else:
                failed.append(uid)
        msg = f"📨 ارسال به {ok} کاربر موفق بود"
        if failed:
            msg += f"، {len(failed)} ناموفق: {', '.join(failed)}"
        await _reply_in_channel(msg)
        return

    # !stats
    if text == "!stats":
        try:
            with open(LOG_FILE) as f:
                entries = [json.loads(l) for l in f if l.strip()]
        except Exception:
            entries = []
        users = _load_users()
        await _reply_in_channel(
            f"📊 آمار:\n"
            f"👥 کاربران: {len(users)}\n"
            f"📝 رویدادهای لاگ: {len(entries)}\n"
            f"🕐 آخرین بهروزرسانی: {_ts()}"
        )
        return

    await _reply_in_channel("دستور ناشناخته. !help بزن.")


async def handle(event_type: str, context: dict):
    try:
        platform = context.get("platform", "")
        # only telegram
        if platform != "telegram":
            return

        user_id = context.get("user_id")
        chat_id = context.get("chat_id")
        chat_type = context.get("chat_type", "")
        username = context.get("username", "") or ""
        first_name = context.get("first_name", "") or ""
        message = context.get("message", "") or ""
        is_channel_cmd = (str(chat_id) == str(LOG_CHANNEL)) or (str(user_id) == "5838175445" and chat_type == "channel")
        # !-prefixed commands from the log channel (gateway would eat /-commands
        # with "Unknown command" before the hook sees them, so we use ! instead)
        bang_cmd = False
        if is_channel_cmd and message.startswith(("!", "／")):  # ASCII or fullwidth
            bang_cmd = True

        # never log the owner's own messages back to the log channel
        if str(user_id) == "5838175445" and str(chat_id) != str(LOG_CHANNEL):
            return

        # 1) remember the user
        if user_id:
            _record_user(user_id, username, first_name, chat_id, chat_type)

        # 2) !-commands from the log channel
        if bang_cmd:
            await _handle_channel_command(message, user_id)
            return

        # 3) forward event to the channel
        # skip echoing our own channel commands
        if is_channel_cmd and message.startswith("/"):
            return

        text = _fmt_event(event_type, context)
        await _reply_in_channel(text)
        _append_log({"ts": _ts(), "event": event_type, "user": user_id, "chat": chat_id, "msg": message[:400]})
    except Exception as e:
        logger.error("log_channel: handler error: %s", str(e)[:200])
