#!/usr/bin/env python3
"""Setup wizard discovery / import / tour tests. Hermetic + offline."""

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

from core import setup_wizard as wz  # noqa: E402
from core import detect  # noqa: E402


class WizardBase(unittest.TestCase):
    """Hermetic: ATROPOS_HOME + HERMES_HOME in tmp; claude side faked via
    detect._home so the real ~/.claude is never read or written."""

    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_wz_")
        os.environ["ATROPOS_HOME"] = self.tmp
        self.home = detect.atropos_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.hh = Path(self.tmp) / "hermes"
        os.environ["HERMES_HOME"] = str(self.hh)
        self.hh.mkdir(parents=True, exist_ok=True)
        self.user = Path(self.tmp) / "user"
        self.user.mkdir(parents=True, exist_ok=True)
        self._p = mock.patch("core.detect._home", return_value=self.user)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)
        if self._h is not None:
            os.environ["HERMES_HOME"] = self._h
        else:
            os.environ.pop("HERMES_HOME", None)

    def _claude_json(self, name="mcp.json", data=None):
        d = self.user / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(json.dumps(data or {"mcpServers": {}}), encoding="utf-8")
        return p


class DiscoverTests(WizardBase):
    def test_summary_shape(self):
        s = wz.discover_summary()
        self.assertTrue(s["ok"])
        self.assertEqual(sorted(s["harnesses"]), ["claude", "hermes"])
        self.assertEqual(sorted(s["groups"]), [
            "commands", "files", "identity", "links", "mcp",
            "memory", "models", "routing", "webhooks"])
        for rec in s["groups"].values():
            self.assertIn("target_exists", rec)
            self.assertIn("claude", rec)
            self.assertIn("hermes", rec)
            self.assertIn("diff", rec)

    def test_group_detection_both_harnesses(self):
        self._claude_json()
        (self.hh / "config.yaml").write_text("mcp:\n  servers: {}\n",
                                             encoding="utf-8")
        s = wz.discover_summary()
        self.assertTrue(s["groups"]["mcp"]["claude"])
        self.assertTrue(s["groups"]["mcp"]["hermes"])
        self.assertFalse(s["groups"]["models"]["claude"])  # no models.json

    def test_which_wins_none_tie_single(self):
        self.assertEqual(wz.which_wins("mcp"), "none")
        self._claude_json()
        (self.hh / "config.yaml").write_text("mcp:\n  servers: {}\n",
                                             encoding="utf-8")
        self.assertEqual(wz.which_wins("mcp"), "tie")
        (self.hh / "config.yaml").write_text("x: 1\n", encoding="utf-8")
        self.assertEqual(wz.which_wins("mcp"), "claude")

    def test_files_group_detection(self):
        fh = Path(os.path.expanduser("~")) / "files-claude"
        if fh.is_dir():
            self.skipTest("~ /files-claude exists on this machine")
        s = wz.discover_summary()
        self.assertFalse(s["groups"]["files"]["claude"])


class ImportTests(WizardBase):
    def test_import_shared_copies_to_atropos(self):
        cd = self.user / ".claude"
        cd.mkdir(parents=True, exist_ok=True)
        (cd / "mcp.json").write_text('{"x": 1}', encoding="utf-8")
        res = wz._import_group("mcp", ["claude"], mode="shared")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["copied"]), 1)
        self.assertTrue((self.home / "mcp" / "mcp.json").exists())
        self.assertEqual((self.home / "mcp" / "mcp.json").read_text(),
                         '{"x": 1}')

    def test_import_per_tags_source(self):
        cd = self.user / ".claude"
        cd.mkdir(parents=True, exist_ok=True)
        (cd / "models.yaml").write_text("a: 1", encoding="utf-8")
        res = wz._import_group("models", ["claude"], mode="per")
        self.assertTrue((self.home / "models" / "claude-models.yaml").exists())
        self.assertTrue(any("claude-" in c for c in res["copied"]))

    def test_import_only_existing(self):
        res = wz._import_group("webhooks", ["both"], mode="shared")
        self.assertEqual(res["copied"], [])  # nothing existed -> nothing copied

    def test_monitor_mode(self):
        res = wz._import_group("mcp", ["claude", "hermes"], mode="monitor")
        self.assertEqual(res["mode"], "monitor")
        self.assertEqual(res["sources"], ["claude", "hermes"])


class DiffTourTests(WizardBase):
    def test_diff_lists_files(self):
        (self.home / "identity").mkdir(parents=True, exist_ok=True)
        (self.home / "identity" / "SOUL.md").write_text("soul", encoding="utf-8")
        rows = wz._diff_group("identity")
        self.assertEqual(len(rows), 1)
        self.assertIn("mtime", rows[0])
        self.assertIn("size", rows[0])

    def test_tour_marks_seen(self):
        self.assertFalse(wz._tour_seen())
        t = wz.tour()
        self.assertEqual(t["steps"], 4)
        self.assertFalse(t["seen"])
        wz.mark_tour_seen()
        self.assertTrue(wz._tour_seen())


if __name__ == "__main__":
    unittest.main()