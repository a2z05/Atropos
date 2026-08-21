#!/usr/bin/env python3
"""Atropos patches — hacks as code, stdlib only.

Each hack is a Python transform function instead of a YAML file: it gets
the pristine adapter.py text and returns the transformed text, plus a
verify marker. The engine applies them in order, ast-checks the result,
and writes only when everything verifies — after every upstream reset,
``atropos patch`` re-arms the customizations.

The old ``hacks/*.yml`` files remain as a **shim** (read for their
id/target/verify only, no editing through them) so existing tests and any
external tooling keep working; the transforms live HERE.

No hardcoded /data or /opt paths: every path comes from detect.py / env.
"""
import ast
import re
import subprocess
from pathlib import Path

from . import config, detect

HACKS_DIR = Path(__file__).resolve().parent.parent / "hacks"
_TARGET = "plugins/platforms/telegram/adapter.py"

# Anchors are the *live* adapter.py text; our transforms then place the
# new code. Keep them byte-exact with the upstream file (see hacks/*.yml
# for the reference copy of what upstream ships).


# ── transforms (each: src → src) ─────────────────────────────────────────
def t_import_typehandler(src: str) -> str:
    return src.replace(
        """        MessageHandler as TelegramMessageHandler,
        ContextTypes,
        filters,
    )""",
        """        MessageHandler as TelegramMessageHandler,
        ContextTypes,
        filters,
        TypeHandler,
    )""",
        1,
    )


def t_guest_hide(src: str) -> str:
    old = 'return getattr(update, "effective_message", None) or getattr(update, "message", None)'
    new = ('if getattr(update, "guest_message", None) is not None:\n'
           "            return None\n"
           + old)
    return src.replace(old, new, 1)


def t_send_suppress(src: str) -> str:
    old = """              if not content or not content.strip():
              return SendResult(success=True, message_id=None)"""
    new = (old + "\n\n"
           "          # Guest-mode suppression: when _handle_guest_message is answering a\n"
           "          # guest query, the gateway must NOT deliver the reply to the event's\n"
           "          # source chat (which for guest messages resolves to the owner DM).\n"
           "          # The guest reply is delivered separately via answer_guest_query.\n"
           "          if getattr(self, \"_suppress_send\", False):\n"
           "              return SendResult(success=True, message_id=None)")
    return src.replace(old, new, 1)


def t_guest_handler(src: str) -> str:
    old = """      event = self._apply_telegram_group_observe_attribution(event)
          self._enqueue_text_event(event)

      async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:"""
    new = """      event = self._apply_telegram_group_observe_attribution(event)
          self._enqueue_text_event(event)


      async def _handle_guest_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
          \"\"\"Answer Telegram Bot API 10 guest summons via answerGuestQuery.\"\"\"
          guest = getattr(update, "guest_message", None)
          if guest is None:
              return
          gqid = getattr(guest, "guest_query_id", None)
          text = getattr(guest, "text", "")
          if not gqid or not text:
              return
          user_id = str(getattr(getattr(guest, "from_user", None), "id", "") or "")
          reply_to = getattr(guest, "reply_to_message", None)
          owner_id = str(
              (self.config.extra.get("allow_from") if getattr(self.config, "extra", None) else None)
              or os.getenv("TELEGRAM_ALLOWED_USERS", "")
              or ""
          ).split(",")[0].strip()
          is_owner = user_id == owner_id and owner_id not in ("", "*")
          if reply_to is None and not is_owner:
              logger.info("[Telegram] Ignored guest mention by user %s (no reply, not owner)", user_id or "?")
              return
          try:
              event = self._build_message_event(guest, MessageType.TEXT, update_id=update.update_id)
          except Exception:
              logger.exception("[Telegram] Failed to build guest event")
              await self._answer_guest_text(gqid, "Give me a second — system hiccup. Try again.")
              return
          event.text = self._clean_bot_trigger_text(event.text or "")
          try:
              from pathlib import Path as _P
              _pf = _P(os.environ.get("HERMES_HOME", "/tmp")) / "assets" / "guest_persona.md"
              _persona = _pf.read_text(encoding="utf-8").strip() if _pf.exists() else ""
          except Exception:
              _persona = ""
          event.channel_prompt = _persona or (
              "You are ATRA, a sovereign AI assistant named after Atropos, the Greek Fate "
              "who cuts the thread of life — 'she who cannot be turned'. You are calm, precise, "
              "and completely honest. Reply in the same language the person used. Keep answers "
              "short and direct — no preamble, no filler, no exclamation marks. "
              "Dry humor is welcome but never forced. You help anyone who asks. "
              "If you don't know something, say so plainly."
          )
          from_user_obj = getattr(guest, "from_user", None)
          _first = str(getattr(from_user_obj, "first_name", "") or "")
          _last = str(getattr(from_user_obj, "last_name", "") or "")
          _uname = str(getattr(from_user_obj, "username", "") or "")
          user_name = (_first + " " + _last).strip() or (f"@{_uname}" if _uname else "?")
          sender_kind = "owner" if is_owner else "guest"
          trigger_kind = "reply" if reply_to is not None else "plain mention"
          identity = (
              f"\\n\\n[Sender identity] user_name={user_name!r} user_id={user_id!r} "
              f"sender={sender_kind} (owner_id={owner_id!r}) trigger={trigger_kind}"
          )
          event.channel_prompt = f"{event.channel_prompt}{identity}"
          event.internal = True
          logger.info(
              "[Telegram] Guest identity: name=%s id=%s sender=%s trigger=%s",
              user_name, user_id or "?", sender_kind, trigger_kind,
          )
          handler = self._message_handler
          if handler is None:
              logger.warning("[Telegram] Guest summon received but no message handler installed")
              await self._answer_guest_text(gqid, self._guest_fallback_reply())
              return
          _prev_suppress = getattr(self, "_suppress_send", False)
          self._suppress_send = True
          try:
              response = await handler(event)
          except Exception as _err:
              logger.exception("[Telegram] Guest message handler failed")
              await self._answer_guest_text(gqid, "Give me a second — system hiccup. Try again.")
              self._suppress_send = _prev_suppress
              return
          reply = str(response) if response else ""
          if not reply.strip():
              reply = self._guest_fallback_reply()
          await self._answer_guest_text(gqid, reply)
          self._suppress_send = _prev_suppress

      def _guest_fallback_reply(self) -> str:
          \"\"\"Minimal fallback when the agent produced no text for a guest query.\"\"\"
          return ""

      async def _answer_guest_text(self, guest_query_id: str, text: str) -> None:
          \"\"\"Answer a guest query with a plain-text article result.\"\"\"
          if not self._bot or not text.strip():
              return
          try:
              from telegram import InlineQueryResultArticle, InputTextMessageContent
              from uuid import uuid4
              await self._bot.answer_guest_query(
                  guest_query_id=guest_query_id,
                  result=InlineQueryResultArticle(
                      id=str(uuid4()),
                      title="Reply",
                      input_message_content=InputTextMessageContent(message_text=text),
                  ),
              )
              logger.info("[Telegram] Answered guest query %s (%d chars)", guest_query_id, len(text))
          except Exception:
              logger.exception("[Telegram] answer_guest_query failed for %s", guest_query_id)

      async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:"""
    return src.replace(old, new, 1) if old in src else src


def t_register_main(src: str) -> str:
    old = """              self._app.add_handler(TelegramMessageHandler(
                  filters.TEXT & ~filters.COMMAND,
                  self._handle_text_message
              ))"""
    new = """              if TELEGRAM_AVAILABLE:
                  try:
                      _guest_filter = getattr(filters.UpdateType, 'GUEST_MESSAGE', None)
                      if _guest_filter is not None:
                          self._app.add_handler(TelegramMessageHandler(
                              _guest_filter,
                              self._handle_guest_message
                          ))
                  except Exception as exc:
                      logger.warning("[Telegram] Guest handler registration failed: %s", exc)
              _non_guest = filters.TEXT & ~filters.COMMAND
              _non_guest = _non_guest & ~filters.UpdateType.GUEST_MESSAGE if hasattr(filters.UpdateType, 'GUEST_MESSAGE') else _non_guest
              self._app.add_handler(TelegramMessageHandler(
                  _non_guest,
                  self._handle_text_message
              ))"""
    return src.replace(old, new, 1)


def t_register_rebuild(src: str) -> str:
    old = """                              self._app.add_handler(TelegramMessageHandler(
                                  filters.TEXT & ~filters.COMMAND,
                                  self._handle_text_message
                              ))"""
    new = """                              if TELEGRAM_AVAILABLE:
                                  try:
                                      _guest_filter = getattr(filters.UpdateType, 'GUEST_MESSAGE', None)
                                      if _guest_filter is not None:
                                          self._app.add_handler(TelegramMessageHandler(
                                              _guest_filter,
                                              self._handle_guest_message
                                          ))
                                  except Exception as exc:
                                      logger.warning("[Telegram] Guest handler registration failed: %s", exc)
                              _non_guest = filters.TEXT & ~filters.COMMAND
                              _non_guest = _non_guest & ~filters.UpdateType.GUEST_MESSAGE if hasattr(filters.UpdateType, 'GUEST_MESSAGE') else _non_guest
                              self._app.add_handler(TelegramMessageHandler(
                                  _non_guest,
                                  self._handle_text_message
                              ))"""
    return src.replace(old, new, 1)


def t_reaction_bridge(src: str) -> str:
    old = """    async def on_processing_start(self, event: MessageEvent) -> None:"""
    new = """    async def add_reaction(self, chat_id: str, emoji: str, message_id=None) -> bool:
        \"\"\"Add a single emoji reaction (bridge for send_message react).\"\"\"
        if not message_id:
            return False
        return await self._set_reaction(chat_id, message_id, emoji)

    async def remove_reaction(self, chat_id: str, message_id=None) -> bool:
        \"\"\"Remove all reactions (bridge for send_message unreact).\"\"\"
        if not message_id:
            return False
        return await self._clear_reactions(chat_id, message_id)

    async def on_processing_start(self, event: MessageEvent) -> None:"""
    return src.replace(old, new, 1)


def t_reaction_start(src: str) -> str:
    # a loading-style reaction while working (burning = thinking)
    return src.replace(
        'await self._set_reaction(chat_id, message_id, "\\U0001f440")',
        'await self._set_reaction(chat_id, message_id, "\\U0001f525")',
        1,
    )


def t_reaction_done(src: str) -> str:
    return src.replace(
        '"\\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\\U0001f44e"',
        '"\\u2705" if outcome == ProcessingOutcome.SUCCESS else "\\U0001f44e"',
        1,
    )


def t_dm_guard(src: str) -> str:
    old = """          if not self._is_user_authorized_from_message(msg):
              logger.warning(
                  "[Telegram] Blocked unauthorized user %s in chat %s",
                  getattr(getattr(msg, "from_user", None), "id", None),
                  getattr(getattr(msg, "chat", None), "id", None),
              )
              return"""
    new = (old + """\n          # DM session-ownership guard (guest-mode artifact). In a private
          # chat Telegram always has chat.id == from_user.id. A mismatch means
          # PTB mislabeled a guest message with the owner identity (or vice
          # versa) — dropping it here prevents guest messages from being
          # persisted into the owner DM session.
          _chat_obj = getattr(msg, "chat", None)
          _usr_obj = getattr(msg, "from_user", None)
          if (
              _chat_obj and _usr_obj
              and str(getattr(_chat_obj, "type", "") or "").lower() in ("private", "dm")
          ):
              _cid = str(getattr(_chat_obj, "id", "") or "")
              _uid = str(getattr(_usr_obj, "id", "") or "")
              if _cid and _uid and _cid != _uid:
                  logger.info("[Telegram] Dropped DM chat/user mismatch (guest artifact) chat=%s user=%s", _cid, _uid)
                  return""")
    return src.replace(old, new, 1)


def t_log_channel(src: str) -> str:
    old = """          return
          if not self._should_process_message(msg):"""
    new = """          return
          # log-channel !-command interception
          _log_ch = getattr(self, "_log_channel_id", None)
          if _log_ch is None:
              _log_ch = getattr(self, "_LOG_CHANNEL_ID", None)
          if _log_ch is None:
              try:
                  _log_ch = int(os.environ.get("ATRA_LOG_CHANNEL", ""))
                  self._log_channel_id = _log_ch
              except Exception:
                  _log_ch = None
          if _log_ch:
              try:
                  _chat_id_val = int(str(getattr(getattr(msg, "chat", None), "id", "") or "0"))
              except (ValueError, TypeError):
                  _chat_id_val = 0
              if _chat_id_val == _log_ch and isinstance(getattr(msg, "text", None), str) and msg.text.startswith("!"):
                  logger.info("[Telegram] !-command intercepted in log channel: %s", msg.text[:80])
                  hook_dir = os.environ.get("HERMES_HOME", "")
                  if hook_dir:
                      import importlib.util as _ilu, sys as _sys
                      _spec = _ilu.spec_from_file_location(
                          "log_channel_hook", f"{hook_dir}/hooks/log_channel/handler.py"
                      )
                      if _spec and _spec.loader:
                          _mod = _ilu.module_from_spec(_spec)
                          _sys.modules["log_channel_hook"] = _mod
                          _spec.loader.exec_module(_mod)
                          await _mod.handle(
                              "command:bang",
                              {
                                  "platform": "telegram",
                                  "user_id": getattr(getattr(msg, "from_user", None), "id", None),
                                  "chat_id": _chat_id_val,
                                  "chat_type": getattr(getattr(msg, "chat", None), "type", None) or "",
                                  "username": getattr(getattr(msg, "from_user", None), "username", None) or "",
                                  "first_name": getattr(getattr(msg, "from_user", None), "first_name", None) or "",
                                  "message": getattr(msg, "text", None) or "",
                              },
                          )
                  return
          if not self._should_process_message(msg):"""
    return src.replace(old, new, 1)


def t_guest_notify(src: str) -> str:
    old = """          if not self._is_user_authorized_from_message(msg):"""
    new = """          try:
              import sys as _sys
              _scripts = str(detect_hermes_home)  # = HERMES_HOME
              _scripts_dir = os.path.join(_scripts, "scripts") if _scripts else ""
              if _scripts_dir and _scripts_dir not in _sys.path:
                  _sys.path.insert(0, _scripts_dir)
              import guest_notify as _guest_notify
              await _guest_notify.notify(
                  user_id=getattr(getattr(msg, "from_user", None), "id", None),
                  username=getattr(getattr(msg, "from_user", None), "username", None) or "",
                  first_name=getattr(getattr(msg, "from_user", None), "first_name", None) or "",
                  chat_id=getattr(getattr(msg, "chat", None), "id", None),
                  chat_type=getattr(getattr(msg, "chat", None), "type", None) or "",
                  msg=getattr(msg, "text", None) or "",
                  reason="unauthorized",
              )
          except Exception as _gn_err:
              logger.debug("[Telegram] guest_notify failed: %s", _gn_err)
          if not self._is_user_authorized_from_message(msg):"""
    return src.replace(old, new, 1)


# ── registry: id → (transform, target, verify markers, apply_after, guest-gated) ──
HACKS = [
    ("import TypeHandler", t_import_typehandler, _TARGET, ["TypeHandler,"], None, False),
    ("effective update message extra", t_guest_hide, _TARGET,
     ['getattr(update, "guest_message", None)'], None, False),
    ("send suppress", t_send_suppress, _TARGET, ["_suppress_send"], None, False),
    ("guest handler block", t_guest_handler, _TARGET,
     ["_handle_guest_message"], None, False),
    ("register main", t_register_main, _TARGET, ["if TELEGRAM_AVAILABLE:"], None, False),
    ("register rebuild", t_register_rebuild, _TARGET, ["_guest_filter"], None, False),
    ("reaction bridge", t_reaction_bridge, _TARGET, ["add_reaction"], "guest handler block", False),
    ("processing start reaction", t_reaction_start, _TARGET, ["if chat_id and message_id:"], "reaction bridge", False),
    ("processing done success", t_reaction_done, _TARGET, ["\\u2705"], None, False),
    ("p8 dm chat/user mismatch guard", t_dm_guard, _TARGET, ["DM session-ownership guard"], None, False),
    ("p10 log-channel !-command intercept", t_log_channel, _TARGET, ["!-command interception"], None, False),
    ("p9 guest notify on unauthorized", t_guest_notify, _TARGET, ["guest_notify"], None, False),
]
# 9+2 guest-gated
_GUEST_GATED = {"effective update message extra", "send suppress", "guest handler block",
                "register main", "register rebuild", "p9 guest notify on unauthorized"}


def load_hacks() -> list:
    """Hack descriptors — pure code registry, no YAML files."""
    out = []
    for item in HACKS:
        if isinstance(item, dict):
            d = dict(item)
            d.setdefault("verify", [])
            out.append(d)
            continue
        hid, fn, target, verify, after, _ = item
        out.append({"id": hid, "fn": fn, "target": target, "verify": verify,
                    "apply_after": after})
    return out


def _guest_gated_ids():
    from . import guest
    return set() if guest.is_enabled() else _GUEST_GATED


def _pristine(target_rel: str) -> str:
    repo = detect.hermes_agent()
    if not repo:
        raise FileNotFoundError("hermes-agent not found")
    rel = target_rel.lstrip("/")
    out = subprocess.run(
        ["git", "-C", repo, "show", f"HEAD:{rel}"],
        capture_output=True, timeout=30, check=True,
    )
    return out.stdout.decode("utf-8", errors="replace")


def _target_path(target_rel: str) -> Path:
    repo = detect.hermes_agent()
    if not repo:
        raise FileNotFoundError("hermes-agent not found")
    return Path(repo) / target_rel.lstrip("/")


def apply_hacks(hacks=None, target=None, write=True, force_guest=False):
    """Apply hacks to a target file (default adapter.py). Returns (applied, skipped, errors)."""
    hacks = hacks or load_hacks()
    gated = set() if force_guest else _guest_gated_ids()
    by_target = {}
    for h in hacks:
        by_target.setdefault(h.get("target", _TARGET), []).append(h)

    applied, skipped, errors = [], [], []
    for t, hs in by_target.items():
        if target and t != target:
            continue
        try:
            src = _pristine(t)
        except Exception as e:
            errors.append(f"{t}: pristine fetch failed: {e}")
            continue
        for h in hs:
            if h["id"] in gated:
                skipped.append((h["id"], "guest mode disabled"))
                continue
            fn = h.get("fn")
            if fn is None:
                # YAML-only legacy path (transforms present → skip)
                skipped.append((h["id"], "no transform for this hack — stale YAML"))
                continue
            try:
                out = fn(src)
            except Exception as e:
                errors.append(f"{h['id']}: transform raised: {e}")
                continue
            if out == src:
                skipped.append((h["id"], "anchor not found (upstream changed?)"))
                continue
            src = out
            applied.append(h["id"])
        ok = True
        for h in hs:
            for g in (h.get("verify") or []):
                if g and src and g not in src:
                    errors.append(f"{h['id']}: verify grep missing: {g}")
                    ok = False
        if t.endswith(".py") and ok:
            try:
                ast.parse(src)
            except SyntaxError as e:
                errors.append(f"{t}: AST parse failed after patches: {e}")
                ok = False
        if ok and write:
            _target_path(t).parent.mkdir(parents=True, exist_ok=True)
            _target_path(t).write_text(src, encoding="utf-8")
    return applied, skipped, errors


def verify():
    """Check which hacks are currently applied to the live files (no write)."""
    hacks = load_hacks()
    results = []
    for h in hacks:
        t = h.get("target", _TARGET)
        try:
            content = _target_path(t).read_text(errors="ignore")
            marker = h.get("verify", [])
            ok = all(g in content for g in marker if g)
            results.append({"id": h["id"], "applied": ok, "target": t})
        except Exception as e:
            results.append({"id": h["id"], "applied": False, "error": str(e)})
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.verify:
        for r in verify():
            print(f"  {'✅' if r['applied'] else '❌'} {r['id']} -> {r.get('target','')}")
    else:
        applied, skipped, errors = apply_hacks()
        print(f"applied: {len(applied)}: {applied}")
        if skipped:
            print(f"skipped: {skipped}")
        if errors:
            print(f"errors: {errors}")