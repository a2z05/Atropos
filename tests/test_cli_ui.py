#!/usr/bin/env python3
"""CLI UI tests — menu, prompts, tables, progress, lore, session names."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import cliui


class TableTests(unittest.TestCase):
    def test_table_renders_aligned(self):
        t = cliui.table([["a", "b"], ["ccc", "d"]], headers=["x", "y"], max_w=60)
        self.assertIn("x", t)
        self.assertIn("ccc", t)

    def test_table_truncates_to_width(self):
        long = [["x" * 200, "y"]]
        t = cliui.table(long, max_w=40)
        lines = t.splitlines()
        self.assertLessEqual(len(lines[0]), 40)

    def test_table_json_mode(self):
        t = cliui.table([{"a": 1}], json_mode=True)
        self.assertEqual(json.loads(t), [{"a": 1}])

    def test_table_empty(self):
        self.assertEqual(cliui.table([], max_w=40), "")


class PromptTests(unittest.TestCase):
    def test_confirm_default_yes(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(cliui.confirm("go?", default=True))

    def test_confirm_no(self):
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(cliui.confirm("go?"))

    def test_confirm_yes(self):
        with mock.patch("builtins.input", return_value="y"):
            self.assertTrue(cliui.confirm("go?"))

    def test_select_by_number(self):
        with mock.patch("builtins.input", return_value="2"):
            self.assertEqual(cliui.select("pick", ["a", "b", "c"]), 1)

    def test_select_by_prefix(self):
        with mock.patch("builtins.input", return_value="ch"):
            self.assertEqual(cliui.select("pick", ["chat", "backup"]), 0)


class LoreTests(unittest.TestCase):
    def test_oracle_line_nonempty(self):
        self.assertTrue(cliui.oracle_line("en"))

    def test_oracle_stable_per_day(self):
        a = cliui.oracle_line("en")
        b = cliui.oracle_line("en")
        self.assertEqual(a, b)

    def test_doctor_verdicts(self):
        self.assertIn("sound", cliui.doctor_verdict(0))
        self.assertIn("frayed", cliui.doctor_verdict(1))
        self.assertIn("tearing", cliui.doctor_verdict(3))

    def test_tip_nonempty(self):
        self.assertTrue(cliui.tip("en"))

    def test_session_name_deterministic(self):
        self.assertEqual(cliui.session_name(5), cliui.session_name(5))
        self.assertIn("thread", cliui.session_name(0))


class ProgressTests(unittest.TestCase):
    def test_progress_non_tty_no_crash(self):
        with mock.patch.object(cliui.sys.stdout, "isatty", return_value=False):
            items = list(cliui.progress(range(3), "working"))
        self.assertEqual(items, [0, 1, 2])


class MenuTests(unittest.TestCase):
    def test_menu_exit(self):
        calls = []
        with mock.patch("builtins.input", return_value="q"):
            cliui.menu(lambda cmd: calls.append(cmd))
        self.assertEqual(calls, [])

    def test_menu_dispatch(self):
        calls = []
        with mock.patch("builtins.input", side_effect=["1", "", "q"]):
            cliui.menu(lambda cmd: calls.append(cmd))
        self.assertEqual(calls, ["status"])


if __name__ == "__main__":
    unittest.main()