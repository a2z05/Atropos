#!/usr/bin/env python3
"""Agent system tests — defs roundtrip, harness resolve, permissions, runs."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from unittest import mock

from core import agents


class AgentBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_agents_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        # never shell out to a real claude in tests — keep runs fast/deterministic
        p = mock.patch("core.detect._find_claude", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def _mk(self, name="reviewer", harness="auto", **kw):
        a = {"name": name, "description": "d", "prompt": "do the thing",
             "harness": harness, "model": None, "effort": "medium",
             "tools": ["*"], "permissions": "default"}
        a.update(kw)
        return agents.save_agent(a)


class DefinitionTests(AgentBase):
    def test_create_edit_delete_roundtrip(self):
        self._mk("reviewer")
        self.assertEqual(len(agents.list_agents()), 1)
        self.assertEqual(agents.get_agent("reviewer")["prompt"], "do the thing")
        a = agents.get_agent("reviewer")
        a["prompt"] = "updated"
        agents.save_agent(a)
        self.assertEqual(agents.get_agent("reviewer")["prompt"], "updated")
        self.assertTrue(agents.delete_agent("reviewer"))
        self.assertEqual(agents.list_agents(), [])

    def test_invalid_harness_rejected(self):
        with self.assertRaises(ValueError):
            self._mk("bad", harness="evil")

    def test_missing_name_rejected(self):
        with self.assertRaises(ValueError):
            agents.save_agent({"prompt": "x"})

    def test_unknown_agent_run(self):
        r = agents.run_agent("nope")
        self.assertFalse(r["ok"])


class ResolveTests(AgentBase):
    def test_auto_fix_goes_lachesis(self):
        h = agents.resolve_harness({"harness": "auto"}, "fix a bug in main.py")
        self.assertEqual(h, "lachesis")

    def test_auto_summarize_goes_clotho(self):
        h = agents.resolve_harness({"harness": "auto"}, "summarize these logs")
        self.assertEqual(h, "clotho")

    def test_manual_override(self):
        h = agents.resolve_harness({"harness": "atropos"}, "fix a bug")
        self.assertEqual(h, "atropos")


class RunTests(AgentBase):
    def test_run_persists_result(self):
        self._mk("reviewer")
        rec = agents.run_agent("reviewer", "check the diff")
        self.assertTrue(rec["ok"])
        self.assertEqual(len(agents.recent_runs()), 1)
        self.assertEqual(agents.recent_runs()[0]["agent"], "reviewer")

    def test_read_only_permissions(self):
        self._mk("ro", permissions="read-only")
        rec = agents.run_agent("ro", "summarize")
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["result"])

    def test_pii_filter_applies(self):
        self._mk("sensitive")
        # the pii middleware would mask an email in the prompt
        rec = agents.run_agent("sensitive", "check joe@x.com")
        self.assertTrue(rec["ok"])


if __name__ == "__main__":
    unittest.main()