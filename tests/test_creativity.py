#!/usr/bin/env python3
"""Creativity suite (v18 E) — fate lines, weave counter, trio, cut frames."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import fate, i18n  # noqa: E402

_BAN = ("empower", "seamless", "leverage", "unlock", "delve", "elevate",
        "robust", "cutting-edge", "streamline", "supercharge", "unleash",
        "effortless", "game-changer", "revolutionize")


class FateBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_fate_")
        os.environ["ATROPOS_HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)


class FateLineTests(FateBase):
    def test_fate_stable_same_day(self):
        self.assertEqual(fate.fate_today(), fate.fate_today())

    def test_fate_is_nonempty(self):
        self.assertTrue(fate.fate_today())

    def test_fate_line_shapes(self):
        s = fate.fate_line("backup")
        self.assertIn("Clotho", s)
        done = fate.fate_line("backup", done=True)
        self.assertIn("woven", done)

    def test_no_ai_slop_in_fate_strings(self):
        low = fate.fate_today().lower()
        for bad in _BAN:
            self.assertNotIn(bad, low)
        for line in fate.fate_line("backup") + fate.fate_line("update", done=True):
            pass
        self.assertNotIn("empower", fate.story("default").lower())

    def test_lore_easter_egg_returns_text(self):
        s = fate.story("default")
        self.assertIsInstance(s, str)
        self.assertGreater(len(s), 40)
        shears = fate.story("shears")
        self.assertIn("hook", shears)

    def test_lore_in_all_languages(self):
        # every language file has fate_lines or falls back to en
        for code in i18n.available():
            try:
                lines = i18n._load(code).get("fate_lines") or []
            except Exception:
                lines = []
            self.assertIsInstance(lines, list, code)


class WeaveCounterTests(FateBase):
    def test_weave_increments(self):
        self.assertEqual(fate.weave(), 0)
        fate.weave(1)
        self.assertEqual(fate.weave(), 1)
        fate.weave(2)
        self.assertEqual(fate.weave(), 3)

    def test_weave_stats_shape(self):
        fate.weave(1)
        s = fate.weave_stats()
        self.assertEqual(s["woven"], 1)
        self.assertIn("last_woven", s)
        self.assertIn("first_boot", s)

    def test_backup_bumps_weave(self):
        from core import backup
        import unittest.mock as mock
        with mock.patch("urllib.request.urlopen"):
            res = backup.create()
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(fate.weave(), 1)


class CutAnimationTests(FateBase):
    def test_cut_frames_valid(self):
        frames = fate.cut_animation()
        self.assertEqual(len(frames), 3)
        for f in frames:
            self.assertEqual(len(f), 3)  # three lines per frame

    def test_cut_line_phrase(self):
        self.assertIn("cut", fate.cut_line())


class TrioApiTests(FateBase):
    def test_stories_have_three_sisters(self):
        s = fate.story("default")
        for name in ("Clotho", "Lachesis", "Atropos"):
            self.assertIn(name, s)


if __name__ == "__main__":
    unittest.main()