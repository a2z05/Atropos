#!/usr/bin/env python3
"""Atropos chat engine tests — sessions, sends, transport failure, export."""

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

from core import chat, config, routing, settings  # noqa: E402


def _fake_completion(body: bytes) -> dict:
    """OpenAI-style completion JSON for the request payload."""
    data = json.loads(body.decode("utf-8"))
    return {
        "choices": [{
            "message": {"role": "assistant",
                        "content": f"echo: {data['messages'][-1]['content'][:20]}"}
        }],
        "model": data["model"],
        "usage": {"total_tokens": 7},
    }


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(req, timeout=None):
    return _FakeResponse(json.dumps(_fake_completion(req.data)).encode("utf-8"))


class ChatBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self._key = os.environ.get("OPENAI_API_KEY")
        self.tmp = tempfile.mkdtemp(prefix="atropos_chat_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        os.environ["OPENAI_API_KEY"] = "fake-key"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h),
                        ("OPENAI_API_KEY", self._key)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class SessionTests(ChatBase):
    def test_create_and_list(self):
        sid = chat.create_session("hello world")
        self.assertEqual(len(sid), 32)
        lst = chat.session_list()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], sid)
        self.assertEqual(lst[0]["title"], "hello world")
        self.assertEqual(lst[0]["message_count"], 0)
        self.assertIn("harness", lst[0])

    def test_auto_title(self):
        self.assertEqual(chat.auto_title("one two three four five six seven"), "one two three four five six")
        self.assertEqual(chat.auto_title("just one"), "just one")
        self.assertEqual(chat.auto_title("   "), "Chat")
        self.assertEqual(chat.auto_title(""), "Chat")

    def test_delete_session(self):
        sid = chat.create_session("gone soon")
        self.assertTrue(chat.delete_session(sid))
        self.assertFalse(chat.delete_session(sid))
        self.assertEqual(chat.session_list(), [])

    def test_db_in_atropos_home(self):
        chat.create_session("x")
        self.assertTrue((Path(self.tmp) / "chat.db").exists())

    def test_stats(self):
        sid = chat.create_session("stats")
        st = chat.stats()
        self.assertEqual(st["sessions"], 1)
        self.assertEqual(st["messages"], 0)
        self.assertEqual(st["db"], str(Path(self.tmp) / "chat.db"))


class SendTests(ChatBase):
    def test_send_persists_both_messages(self):
        sid = chat.create_session("demo")
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            res = chat.send(sid, "hello there")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["session_id"], sid)
        self.assertEqual(res["reply"], "echo: hello there")
        self.assertIn("latency_ms", res)
        msgs = chat.session_messages(sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0]["content"], "hello there")
        self.assertEqual(msgs[1]["content"], "echo: hello there")
        self.assertIsInstance(msgs[1]["latency_ms"], int)
        self.assertGreaterEqual(msgs[1]["latency_ms"], 0)
        self.assertEqual(msgs[1]["tokens"], 7)
        sess = chat.session_list()[0]
        self.assertEqual(sess["message_count"], 2)
        self.assertIn("harness", sess)

    def test_send_creates_session_and_auto_title(self):
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            res = chat.send(None, "the quick brown fox jumps over the lazy dog")
        self.assertTrue(res["ok"])
        sid = res["session_id"]
        lst = chat.session_list()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["id"], sid)
        self.assertEqual(lst[0]["title"], "the quick brown fox jumps over")
        self.assertEqual(lst[0]["message_count"], 2)

    def test_routes_through_dispatch_harness(self):
        # "how to fix my python bug" → lachesis (claude), via dispatch
        with mock.patch("urllib.request.urlopen", _fake_urlopen), \
             mock.patch("core.chat.routing.dispatch",
                        return_value={"category": "debugging",
                                      "harness": "lachesis", "by": "default",
                                      "score": {}}) as dsp:
            res = chat.send(None, "how to fix my python bug")
        dsp.assert_called_once_with("how to fix my python bug")
        self.assertTrue(res["ok"])
        self.assertEqual(res["harness"], "lachesis")
        msgs = chat.session_messages(res["session_id"])
        self.assertEqual(msgs[0]["harness"], "lachesis")
        self.assertEqual(msgs[1]["harness"], "lachesis")

    def test_effort_default_and_override(self):
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            r1 = chat.send(None, "hello default effort")
        self.assertEqual(r1["effort"], "medium")
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            r2 = chat.send(None, "hello high effort", effort="high")
        self.assertEqual(r2["effort"], "high")
        msgs = chat.session_messages(r2["session_id"])
        self.assertEqual(msgs[1]["effort"], "high")

    def test_unknown_session_rejected(self):
        res = chat.send("deadbeef" * 4, "hi")
        self.assertFalse(res["ok"])
        self.assertIn("unknown session", res["error"])

    def test_empty_message_rejected(self):
        res = chat.send(None, "   ")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "empty message")

    def test_bad_transport_returns_error_no_crash(self):
        def boom(req, timeout=None):
            raise ConnectionError("network unreachable")
        sid = chat.create_session("flaky")
        with mock.patch("urllib.request.urlopen", boom):
            res = chat.send(sid, "hello")
        self.assertFalse(res["ok"])
        self.assertIn("network unreachable", res["error"])
        # user msg + system error msg both persisted
        msgs = chat.session_messages(sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "system"])
        self.assertIn("network unreachable", msgs[1]["content"])
        # follow-up send with working transport recovers
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            res2 = chat.send(sid, "hello again")
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["reply"], "echo: hello again")

    def test_http_error_reported(self):
        import urllib.error
        def htmlerr(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         None, None)
        with mock.patch("urllib.request.urlopen", htmlerr):
            res = chat.send(None, "hello")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "HTTP 429: Too Many Requests")

    def test_llm_payload_shape(self):
        captured = {}
        def spy(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = req.headers
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(json.dumps(_fake_completion(req.data)).encode("utf-8"))
        with mock.patch("urllib.request.urlopen", spy):
            chat.send(None, "shape check")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["payload"]["model"], "deepmo")
        self.assertEqual(captured["payload"]["max_tokens"], 256)
        self.assertEqual(captured["payload"]["messages"][-1]["role"], "user")
        self.assertEqual(captured["payload"]["messages"][-1]["content"], "shape check")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer fake-key")


class SlashCommandTests(ChatBase):
    def test_slash_routes_to_console_whitelist(self):
        sid = chat.create_session("console")
        with mock.patch("core.chat.console.run_command") as rc:
            rc.return_value = {"ok": True, "command": "version",
                               "output": ["atropos 1.2.0 @ abc123"]}
            res = chat.send(sid, "/version")
        rc.assert_called_once_with("version")
        self.assertTrue(res["ok"])
        self.assertEqual(res["reply"], "atropos 1.2.0 @ abc123")
        self.assertEqual(res["harness"], "atropos")
        msgs = chat.session_messages(sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["harness"], "atropos")

    def test_slash_unknown_command_is_error_not_crash(self):
        # the real whitelist rejects unregistered commands
        res = chat.send(None, "/definitely-not-a-command")
        self.assertFalse(res["ok"])
        self.assertIn("unknown command", res["error"])
        self.assertEqual(res["harness"], "atropos")
        msgs = chat.session_messages(res["session_id"])
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertIn("unknown command", msgs[1]["content"])

    def test_slash_failure_persisted_as_assistant_error(self):
        sid = chat.create_session("console-fail")
        with mock.patch("core.chat.console.run_command") as rc:
            rc.return_value = {"ok": False, "command": "backup",
                               "error": "boom", "output": []}
            res = chat.send(sid, "/backup")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reply"], "boom")
        msgs = chat.session_messages(sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["content"], "boom")


class StreamExportTests(ChatBase):
    def test_stream_yields_delta_then_done(self):
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            events = list(chat.chat_stream(None, "stream me"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "delta")
        self.assertEqual(events[0]["text"], "echo: stream me")
        self.assertEqual(events[1]["event"], "done")
        self.assertTrue(events[1]["ok"])
        self.assertIsNotNone(events[1]["session_id"])
        # the send persisted exactly once
        sess = chat.session_list()[0]
        self.assertEqual(sess["message_count"], 2)

    def test_stream_survives_transport_failure(self):
        def boom(req, timeout=None):
            raise OSError("down")
        with mock.patch("urllib.request.urlopen", boom):
            events = list(chat.chat_stream(None, "hello"))
        self.assertEqual(events[0]["event"], "delta")
        self.assertIn("down", events[0]["text"])
        self.assertEqual(events[1]["event"], "done")
        self.assertFalse(events[1]["ok"])

    def test_export_shape(self):
        sid = chat.create_session("export me")
        with mock.patch("urllib.request.urlopen", _fake_urlopen):
            chat.send(sid, "hello")
            chat.send(sid, "world")
        text = chat.export(sid)
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["role"] for r in rows],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual(rows[0]["session_id"], sid)
        self.assertEqual(rows[2]["content"], "world")
        for r in rows:
            for key in ("id", "session_id", "role", "content", "ts"):
                self.assertIn(key, r)


if __name__ == "__main__":
    unittest.main()
