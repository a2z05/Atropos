#!/usr/bin/env python3
"""Atropos files tests — safe listing, text reads, traversal rejection, search."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import files  # noqa: E402


class FilesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atropos_files_")
        self.root = Path(self.tmp)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.txt").write_text("hello world", encoding="utf-8")
        (self.root / "main.py").write_text("print('hi')", encoding="utf-8")
        (self.root / "data.bin").write_bytes(b"\x00\x01\x02binary\x00")
        (self.root / "notes.txt").write_text("alpha", encoding="utf-8")
        (self.root / "Alpha.Log").write_text("beta", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ListDirTests(FilesBase):
    def test_list_root(self):
        res = files.list_dir("", root=self.root)
        self.assertTrue(res["ok"])
        names = [e["name"] for e in res["entries"]]
        self.assertIn("main.py", names)
        self.assertIn("docs", names)
        docs = next(e for e in res["entries"] if e["name"] == "docs")
        self.assertEqual(docs["type"], "dir")
        self.assertIn("size", docs)
        self.assertIn("mtime", docs)

    def test_list_subdir(self):
        res = files.list_dir("docs", root=self.root)
        self.assertTrue(res["ok"])
        self.assertEqual([e["name"] for e in res["entries"]], ["guide.txt"])
        self.assertEqual(res["entries"][0]["type"], "file")

    def test_list_missing_and_file_targets(self):
        self.assertFalse(files.list_dir("nope", root=self.root)["ok"])
        self.assertFalse(files.list_dir("main.py", root=self.root)["ok"])

    def test_list_bounded_to_200(self):
        for i in range(250):
            (self.root / f"bulk_{i:03d}.txt").write_text("x", encoding="utf-8")
        res = files.list_dir("", root=self.root)
        self.assertTrue(res["ok"])
        self.assertLessEqual(len(res["entries"]), 200)


class ReadFileTests(FilesBase):
    def test_read_text(self):
        res = files.read_file("docs/guide.txt", root=self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "hello world")
        self.assertEqual(res["name"], "guide.txt")
        self.assertEqual(res["size"], 11)

    def test_read_truncates(self):
        (self.root / "big.txt").write_text("z" * 5000, encoding="utf-8")
        res = files.read_file("big.txt", max_bytes=100, root=self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["text"]), 100)
        self.assertTrue(res["truncated"])

    def test_read_rejects_binary(self):
        res = files.read_file("data.bin", root=self.root)
        self.assertFalse(res["ok"])
        self.assertIn("binary", res["error"])

    def test_read_missing_and_dir(self):
        self.assertFalse(files.read_file("nope.txt", root=self.root)["ok"])
        self.assertFalse(files.read_file("docs", root=self.root)["ok"])


class TraversalTests(FilesBase):
    def test_dotdot_rejected(self):
        res = files.list_dir("../secret", root=self.root)
        self.assertFalse(res["ok"])
        self.assertIn("escapes", res["error"])
        res2 = files.read_file("../secret.txt", root=self.root)
        self.assertFalse(res2["ok"])

    def test_deep_dotdot_rejected(self):
        res = files.list_dir("docs/../../secret", root=self.root)
        self.assertFalse(res["ok"])

    def test_absolute_path_rejected(self):
        for fn in (files.list_dir, files.read_file):
            res = fn(str(Path(self.tmp).parent / "outside"), root=self.root)
            self.assertFalse(res["ok"])

    def test_windows_absolute_rejected(self):
        res = files.list_dir(r"C:\Windows\System32", root=self.root)
        self.assertFalse(res["ok"])

    def test_symlink_escape_rejected(self):
        outside = Path(self.tmp).parent / "files_outside_private"
        outside.mkdir(exist_ok=True)
        try:
            (outside / "data.txt").write_text("secret", encoding="utf-8")
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            res = files.read_file("link/data.txt", root=self.root)
            self.assertFalse(res["ok"])
        except (OSError, NotImplementedError):
            pass  # symlinks unavailable — nothing to verify
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_no_writes_ever(self):
        before = {p.name: p.stat().st_mtime_ns for p in self.root.rglob("*")}
        files.list_dir("", root=self.root)
        files.read_file("main.py", root=self.root)
        files.search("main", root=self.root)
        after = {p.name: p.stat().st_mtime_ns for p in self.root.rglob("*")}
        self.assertEqual(before, after)


class SearchTests(FilesBase):
    def test_search_filename_contains(self):
        res = files.search("guide", root=self.root)
        self.assertTrue(res["ok"])
        self.assertEqual([h.replace("\\", "/") for h in res["hits"]], ["docs/guide.txt"])

    def test_search_case_insensitive(self):
        res = files.search("alpha", root=self.root)
        self.assertEqual([h.replace("\\", "/") for h in res["hits"]], ["Alpha.Log"])

    def test_search_empty_query(self):
        res = files.search("", root=self.root)
        self.assertEqual(res["hits"], [])

    def test_search_no_hits(self):
        res = files.search("zzz-nothing", root=self.root)
        self.assertEqual(res["hits"], [])

    def test_search_bounded(self):
        for i in range(150):
            (self.root / f"hit_{i:03d}.dat").write_text("x", encoding="utf-8")
        res = files.search("hit_", root=self.root)
        self.assertEqual(len(res["hits"]), 100)

    def test_search_bad_root(self):
        res = files.search("x", root=Path(self.tmp) / "missing")
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
