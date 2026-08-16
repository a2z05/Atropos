#!/usr/bin/env python3
"""ASCII identity tests — banner, threads, TUI header."""
import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import ascii


class BannerTests(unittest.TestCase):
    def test_all_rows_same_width(self):
        rows = ascii.banner(color=False).splitlines()
        self.assertEqual(len({len(r) for r in rows}), 1)

    def test_contains_all_letter_shapes_and_shears(self):
        b = ascii.banner(color=False)
        # one glyph per letter of the wordmark: A row1 starts "█▄▄█", S ends "▀  ▀"
        self.assertIn("█▄▄█   █", b)  # A + T column
        self.assertIn("█▀▀▀", b)  # R + P
        self.assertIn("▀  ▀", b)  # O/S base
        self.assertIn("✂", b)  # shears

    def test_no_color_env(self):
        with _env("NO_COLOR", "1"):
            b = ascii.banner()
            self.assertNotIn("\x1b[", b)

    def test_zero_width_ok(self):
        h = ascii.tui_header("1.4.1", width=0)
        self.assertIn("CLOTHO · LACHESIS · ATROPOS", h)

    def test_wordmark_fallback(self):
        w = ascii.wordmark()
        self.assertIn("█▄▄█   █", w)
        self.assertIn("✂", w)

    def test_threads_ornament(self):
        t = ascii.threads()
        self.assertIn("✂", t)
        self.assertEqual(len(t.splitlines()), 5)


class TuiHeaderTests(unittest.TestCase):
    def test_frame_and_trio(self):
        h = ascii.tui_header("1.4.1-beta", width=100)
        self.assertIn("╔", h)  # ╔
        self.assertIn("╝", h)  # ╝
        self.assertIn("CLOTHO · LACHESIS · ATROPOS", h)
        self.assertIn("1.4.1-beta", h)

    def test_theme_accent(self):
        h = ascii.tui_header("1.4.1", theme="matrix", width=80)
        self.assertIn("[92m", h)

    def test_theme_accent_respects_no_color(self):
        with _env("NO_COLOR", "1"):
            h = ascii.tui_header("1.4.1", theme="matrix", width=80)
            self.assertNotIn("\x1b[", h)

    def test_row_widths_uniform(self):
        h = ascii.tui_header("1.4.1", width=80)
        lines = [l for l in h.splitlines() if "║" in l]
        self.assertEqual(len({len(l) for l in lines}), 1)


class _env:
    def __init__(self, k, v):
        self.k, self.v, self._old = k, v, os.environ.get(k)

    def __enter__(self):
        os.environ[self.k] = self.v

    def __exit__(self, *a):
        if self._old is None:
            os.environ.pop(self.k, None)
        else:
            os.environ[self.k] = self._old


if __name__ == "__main__":
    unittest.main()