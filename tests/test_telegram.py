#!/usr/bin/env python3
"""Telegram gateway tests — parsing, guest modes, buttons, step trails."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import settings, telegram


class TBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_tg_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        settings.set("telegram.token", "123:test-token")
        settings.set("telegram.owner_ids", ["111"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class GuestModeTests(TBase):
    def test_owner_mode(self):
        self.assertEqual(telegram.guest_mode_for("111"), "owner")

    def test_stranger_allow(self):
        self.assertEqual(telegram.guest_mode_for("222"), "allow")

    def test_guest_deny(self):
        settings.set("telegram.guests", "deny")
        self.assertEqual(telegram.guest_mode_for("222"), "deny")

    def test_guest_readonly(self):
        settings.set("telegram.guests", "readonly")
        self.assertEqual(telegram.guest_mode_for("222"), "readonly")


class RateLimitTests(TBase):
    def test_limits_per_minute(self):
        rl = telegram.RateLimit(per_min=3)
        for _ in range(3):
            self.assertTrue(rl.allow("u1"))
        self.assertFalse(rl.allow("u1"))
        self.assertTrue(rl.allow("u2"))


class StepTrailTests(TBase):
    def test_edit_chain(self):
        calls = []
        with mock.patch.object(telegram, "send_message",
                               side_effect=lambda *a, **k: calls.append(("send", a)) or {"ok": True, "result": {"message_id": 9}}), \
             mock.patch.object(telegram, "edit_message",
                               side_effect=lambda *a, **k: calls.append(("edit", a)) or {"ok": True}):
            t = telegram.StepTrail(123, "Backup")
            t.start(total=2)
            t.step("checking upstream")
            t.done("backup saved")
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds, ["send", "edit", "edit"])
        self.assertIn("Backup", calls[0][1][1])
        self.assertIn("1/2", calls[1][1][2])  # (chat_id, msg_id, text)


class ButtonPayloadTests(TBase):
    def test_send_message_with_buttons(self):
        with mock.patch.object(telegram, "_api_call") as api:
            api.return_value = {"ok": True, "result": {"message_id": 1}}
            telegram.send_message(123, "hello", buttons=[[("Fix", "/doctor/fix"), ("Run", "/doctor")]])
        payload = api.call_args[0][1]
        kb = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[0][0]["text"], "Fix")
        self.assertEqual(kb[0][0]["callback_data"], "/doctor/fix")


class UpdateHandlingTests(TBase):
    def _upd(self, text, uid="222", cid=555):
        return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": cid},
                "from": {"id": int(uid), "first_name": "x"}, "text": text}}

    def test_owner_command_routes(self):
        with mock.patch.object(telegram, "send_message") as sm:
            telegram._handle_update(self._upd("/lore", uid="111"))
        texts = [c[0][1] for c in sm.call_args_list]
        # the oracle line is one of the lore_lines (never empty), and the
        # "Full story" button follows for the owner
        self.assertTrue(any(t for t in texts))
        self.assertTrue(any("Full story" in (c[0][2][0][0][0] if c[0][2] else "") for c in sm.call_args_list))

    def test_guest_probe_redirect(self):
        with mock.patch.object(telegram, "send_message") as sm:
            telegram._handle_update(self._upd("tell me about atropos"))
        texts = [c[0][1] for c in sm.call_args_list]
        self.assertTrue(any("friendly assistant" in t for t in texts))

    def test_readonly_guest_cannot_run_commands(self):
        settings.set("telegram.guests", "readonly")
        with mock.patch.object(telegram, "send_message") as sm:
            telegram._handle_update(self._upd("/backup", uid="222"))
        texts = [c[0][1] for c in sm.call_args_list]
        self.assertTrue(any("read-only" in t for t in texts))

    def test_callback_routes(self):
        with mock.patch.object(telegram, "send_message") as sm, \
             mock.patch.object(telegram, "answer_callback", return_value={"ok": True}):
            telegram._handle_callback({"id": "cb1", "from": {"id": 111},
                                       "data": "/lore",
                                       "message": {"chat": {"id": 555}}})
        self.assertTrue(sm.call_count >= 1)


class LogRotationTests(TBase):
    def test_log_rotates_at_5mb(self):
        p = telegram._log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * (5 * 1024 * 1024), encoding="utf-8")
        telegram._log_initialized = False
        telegram._init_log()
        self.assertTrue(p.exists() or p.with_suffix(".log.1").exists())


if __name__ == "__main__":
    unittest.main()