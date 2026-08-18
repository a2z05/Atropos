#!/usr/bin/env python3
"""v18 G tests — Telegram bot ops as gated agent tools.

The 20-odd Bot API methods in core/telegram.bot_op are covered here:
owner-only default, per-chat allowlist opt-in, two-step confirm for
destructive ops with a 60s window, and API error envelopes. The Telegram
HTTP layer is mocked so no network is ever touched.
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

_REPO = __import__("pathlib").Path(__file__).resolve().parent.parent
import sys
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import telegram


class _Hermetic(unittest.TestCase):
    _SNIP = ("HERMES_HOME", "ATROPOS_HOME")

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in self._SNIP}
        for k in self._SNIP:
            os.environ.pop(k, None)
        self._home = tempfile.mkdtemp(prefix="atropos_botops_")
        os.environ["ATROPOS_HOME"] = self._home
        import importlib
        import core.settings as _s
        importlib.reload(_s)
        _s.set("telegram.token", "test-token-123")
        _s.set("telegram.owner_ids", ["111"])
        telegram.confirm_state.clear()
        # settings.reload keeps config.config_path() resolving to the fresh home.

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("ATROPOS_HOME", None)
        telegram.confirm_state.clear()
        shutil.rmtree(self._home, ignore_errors=True)

    def _mock_api(self, result=None, ok=True, description=""):
        m = mock.patch.object(telegram, "_api_call")
        fake = m.start()
        fake.return_value = {"ok": ok,
                            "result": result if result is not None else {},
                            "description": description}
        self.addCleanup(m.stop)
        return fake


class OpsGateTests(_Hermetic):
    def test_owner_allowed(self):
        allowed, why = telegram.ops_allowed("111", chat_id=123)
        self.assertTrue(allowed)
        self.assertEqual(why, "owner")

    def test_guest_denied_by_default(self):
        allowed, why = telegram.ops_allowed("222", chat_id=123)
        self.assertFalse(allowed)
        self.assertIn("owner-only", why)

    def test_per_chat_allowlist_grants(self):
        import core.settings as _s
        _s.set("telegram.ops_allowed", ["123"])
        allowed, why = telegram.ops_allowed("222", chat_id="123")
        self.assertTrue(allowed)
        self.assertIn("allowlist", why)
        # different chat still denied
        denied, _ = telegram.ops_allowed("222", chat_id="999")
        self.assertFalse(denied)

    def test_allow_all_grants_everywhere(self):
        import core.settings as _s
        _s.set("telegram.ops_allowed", ["all"])
        allowed, _ = telegram.ops_allowed("222", chat_id="7")
        self.assertTrue(allowed)


class BotOpGateTests(_Hermetic):
    def test_no_token_configured(self):
        import core.settings as _s
        _s.set("telegram.token", "")
        res = telegram.bot_op("get_me", {}, "111", 1)
        self.assertFalse(res["ok"])
        self.assertIn("token", res["error"])

    def test_unknown_op_rejected(self):
        res = telegram.bot_op("frobnicate", {}, "111", 1)
        self.assertFalse(res["ok"])
        self.assertIn("unknown", res["error"])

    def test_non_owner_denied(self):
        res = telegram.bot_op("get_me", {}, "222", 1)
        self.assertFalse(res["ok"])
        self.assertIn("owner-only", res["error"])

    def test_owner_safe_op_calls_api(self):
        self._mock_api(result={"id": 42, "username": "moirai_bot"})
        res = telegram.bot_op("get_me", {}, "111", 1)
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["id"], 42)

    def test_destructive_op_requires_confirm(self):
        fake = self._mock_api(result={"message_id": 9})
        # first call returns need_confirm token, no API call
        res = telegram.bot_op("delete_message",
                              {"chat_id": 1, "message_id": 9}, "111", 1)
        self.assertFalse(res["ok"])
        self.assertIn("need_confirm", res)
        token = res["need_confirm"]
        self.assertEqual(len(str(token)), 6)
        self.assertFalse(fake.call_count)
        # second call without token → still pending, empty need_confirm
        again = telegram.bot_op("delete_message",
                                {"chat_id": 1, "message_id": 9}, "111", 1)
        self.assertIn("need_confirm", again)
        self.assertEqual(again["need_confirm"], "")
        # third call with the token → passes through
        ok = telegram.bot_op("delete_message",
                             {"chat_id": 1, "message_id": 9, "confirm": token},
                             "111", 1)
        self.assertTrue(ok["ok"])

    def test_confirm_token_expires_after_60s(self):
        self._mock_api(result={})
        res = telegram.bot_op("ban_chat_member", {"chat_id": 1, "user_id": 2},
                              "111", 1)
        token = res["need_confirm"]
        key = f"confirm:1:ban_chat_member"
        telegram.confirm_state[key]["ts"] = int(time.time()) - 61
        denied = telegram.bot_op("ban_chat_member",
                                 {"chat_id": 1, "user_id": 2, "confirm": token},
                                 "111", 1)
        self.assertFalse(denied["ok"])
        self.assertIn("expired", denied["error"])

    def test_wrong_confirm_token_rejected(self):
        self._mock_api(result={})
        res = telegram.bot_op("leave_chat", {"chat_id": 1}, "111", 1)
        token = res["need_confirm"]
        bad = telegram.bot_op("leave_chat",
                              {"chat_id": 1, "confirm": str((int(token) + 1) % 1000000).zfill(6)},
                              "111", 1)
        self.assertFalse(bad["ok"])

    def test_non_destructive_safe_without_token(self):
        self._mock_api(result={"username": "x"})
        res = telegram.bot_op("get_webhook_info", {}, "111", 1)
        self.assertTrue(res["ok"])

    def test_per_chat_allowlist_member_can_call_safe_ops(self):
        import core.settings as _s
        _s.set("telegram.ops_allowed", ["123"])
        self._mock_api(result={"ok": True})
        res = telegram.bot_op("get_chat", {"chat_id": 123}, "222", chat_id="123")
        self.assertTrue(res["ok"])
        # destructive still gated by confirm even for allowlisted chat
        res2 = telegram.bot_op("delete_message", {"chat_id": 123, "message_id": 1},
                               "222", chat_id="123")
        self.assertIn("need_confirm", res2)

    def test_api_error_enveloped(self):
        self._mock_api(ok=False, description="Bad Request: chat not found")
        res = telegram.bot_op("get_me", {}, "111", 1)
        self.assertFalse(res["ok"])
        self.assertIn("chat not found", res["error"])


class OpsCommandTests(_Hermetic):
    def test_ops_status_lists_methods_and_access(self):
        msgs = telegram.route_command("/ops", 123, "222")
        self.assertEqual(len(msgs), 1)
        text = msgs[0][1]
        self.assertIn("Bot ops", text)
        self.assertIn("owner-only", text)

    def test_ops_allow_then_deny_roundtrip(self):
        import core.settings as _s
        telegram.route_command("/ops allow", 555, "222")
        allowed = _s.get("telegram.ops_allowed")
        self.assertTrue(any(str(x) == "555" for x in allowed))
        telegram.route_command("/ops deny", 555, "222")
        self.assertNotIn("555", _s.get("telegram.ops_allowed") or [])

    def test_ops_bad_usage_hint(self):
        msgs = telegram.route_command("/ops nonsense", 1, "111")
        self.assertIn("Usage", msgs[0][1])


if __name__ == "__main__":
    unittest.main()