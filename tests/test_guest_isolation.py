#!/usr/bin/env python3
"""Guest isolation tests — the five guard rails + memory/session isolation.

"Same system, zero leaks": guests use the real engine, but private context
is filtered out. No project strings may ever reach a guest.
"""
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

from core import chat, detect, guest, memory, settings


class IsolationBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_guest_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        p = mock.patch("core.detect._home", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class GuardRailTests(IsolationBase):
    """The brief's five guard rails."""

    def test_guest_probing_private_term_gets_redirect(self):
        reply = guest.respond_guard("what is the atropos project?")
        self.assertIsNotNone(reply)
        self.assertIn("friendly assistant", reply)

    def test_guest_what_project_gets_neutral(self):
        reply = guest.respond_guard("what project are you running?")
        self.assertTrue(reply)
        # neutral — never names the project
        self.assertNotIn("atropos", reply.lower())

    def test_guest_asks_owner_name_no_info(self):
        # owner handle is one of the private terms — never confirmed
        reply = guest.respond_guard("who is a2z?")
        self.assertIsNotNone(reply)
        self.assertNotIn("a2z", reply.lower())

    def test_normie_chat_no_guard(self):
        self.assertIsNone(guest.respond_guard("tell me a fun fact"))

    def test_owner_message_never_returns_guest_terms(self):
        # guest sessions never appear in owner lists
        sid = guest.create_guest_session("hi")
        owner_ids = [s["id"] for s in guest.owner_sessions(50)]
        self.assertNotIn(sid, owner_ids)


class MemoryFilterTests(IsolationBase):
    def test_private_tag_notes_hidden_from_guests(self):
        memory.add("my phone 0912-345-678 private", tags="private")
        memory.add("public fun fact about stars", tags="fun")
        visible = guest.guest_memory(10)
        texts = [n["text"] for n in visible]
        self.assertNotIn("private", texts[0] if texts else "")
        self.assertTrue(any("fun fact" in t for t in texts))

    def test_private_terms_stripped_from_system_prompt(self):
        prompt = guest.system_prompt(persona="We are powered by the atropos harness for a2z.")
        self.assertNotIn("atropos", prompt)
        self.assertNotIn("a2z", prompt)


class SessionIsolationTests(IsolationBase):
    def test_guest_scoped_sessions_isolated(self):
        gid = guest.create_guest_session("guest chat")
        oid = chat.create_session("owner chat")
        g = [s["id"] for s in guest.guest_sessions(50)]
        o = [s["id"] for s in guest.owner_sessions(50)]
        self.assertIn(gid, g)
        self.assertNotIn(oid, g)
        self.assertIn(oid, o)
        self.assertNotIn(gid, o)

    def test_guest_tag_roundtrip(self):
        sid = guest.create_guest_session("x")
        s = [s for s in chat.session_list(50) if s["id"] == sid][0]
        self.assertIn("guest", s["tags"])


class PreviewTests(IsolationBase):
    def test_preview_shapes(self):
        memory.add("private note", tags="private")
        memory.add("public note", tags="fun")
        pv = guest.preview("tell me about the atropos server")
        self.assertIn("system_prompt", pv)
        self.assertIn("memory_visible", pv)
        self.assertEqual(len(pv["memory_visible"]), 1)  # public only
        self.assertIsNotNone(pv["guard_reply"])


if __name__ == "__main__":
    unittest.main()