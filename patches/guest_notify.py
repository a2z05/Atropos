"""guest_notify: notify the ATRA log group whenever an unauthorized/guest
message is rejected at the Telegram adapter intake.

Standalone module (no hard dependency on hermes internals): any adapter patch
calls notify_guest() at the rejection point; failures are swallowed so a
notification problem can never break message intake.

Patched call sites in plugins/platforms/telegram/adapter.py:
  _handle_text_message:  `if not self._is_user_authorized_from_message(msg):`
  (same for media/command handlers where needed)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("hermes_hook_log_channel")

LOG_GROUP = -1003744718087  # "ATRA log" supergroup
ENV_PATH = "/data/.hermes/.env"
OWNER_ID = "5838175445"


def _token() -> str | None:
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception as e:
        logger.error("guest_notify: env load failed: %s", e)
    return None


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def notify(
    *,
    user_id=None,
    username="",
    first_name="",
    chat_id=None,
    chat_type="",
    msg="",
    reason="unauthorized",
) -> None:
    """Send a log line to the ATRA log group. Never raises."""
    try:
        if str(user_id) == OWNER_ID:
            return  # never log the owner
        token = _token()
        if not token:
            logger.error("guest_notify: no token")
            return
        who = first_name or username or str(user_id) or "?"
        if username:
            who += f" (@{username})"
        text = (
            f"⚠️ {reason} — {_ts()}\n"
            f"👤 {who} (id:{user_id})\n"
            f"📍 chat: {chat_id} ({chat_type})"
        )
        if msg:
            text += f"\n💬 {msg[:300]}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": LOG_GROUP, "text": text, "disable_web_page_preview": True},
            )
            if r.status_code != 200:
                logger.error("guest_notify: tg send -> %s %s", r.status_code, r.text[:120])
    except Exception as e:
        logger.error("guest_notify: failed: %s", str(e)[:150])