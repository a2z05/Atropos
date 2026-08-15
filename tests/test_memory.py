#!/usr/bin/env python3
"""Atropos memory store tests — CRUD, tags, search scoring, empty store."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import detect, memory  # noqa: E402


class MemoryBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_memory_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class EmptyStoreTests(MemoryBase):
    def test_list_empty(self):
        self.assertEqual(memory.list(), [])

    def test_search_empty(self):
        self.assertEqual(memory.search("anything"), [])

    def test_stats_empty(self):
        s = memory.stats()
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["sources"], {})
        self.assertIsNone(s["last_added"])

    def test_notes_path_under_atropos_home(self):
        self.assertEqual(
            memory.notes_path(),
            Path(detect.atropos_home()) / "memory" / "notes.json",
        )

    def test_delete_empty_returns_false(self):
        self.assertFalse(memory.delete("does-not-exist"))

    def test_add_empty_text_raises(self):
        with self.assertRaises(ValueError):
            memory.add("   ")
        with self.assertRaises(ValueError):
            memory.add("")


class AddListTests(MemoryBase):
    def test_add_returns_uuid_hex(self):
        nid = memory.add("hello world")
        self.assertEqual(len(nid), 32)
        int(nid, 16)  # hex

    def test_add_and_list(self):
        nid = memory.add("deploy failed on staging")
        notes = memory.list()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["id"], nid)
        self.assertEqual(notes[0]["text"], "deploy failed on staging")
        self.assertEqual(notes[0]["tags"], [])
        self.assertEqual(notes[0]["source"], "manual")
        self.assertIn("T", notes[0]["ts"])  # ISO timestamp

    def test_list_newest_first(self):
        first = memory.add("first note")
        second = memory.add("second note")
        third = memory.add("third note")
        notes = memory.list()
        self.assertEqual([n["id"] for n in notes], [third, second, first])

    def test_list_limit(self):
        for i in range(10):
            memory.add(f"note number {i}")
        notes = memory.list(limit=3)
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0]["text"], "note number 9")

    def test_add_tags_list_and_string(self):
        nid1 = memory.add("deploy fix", tags=["devops", "urgent"])
        nid2 = memory.add("research summary", tags="research, notes")
        notes = {n["id"]: n for n in memory.list()}
        self.assertEqual(notes[nid1]["tags"], ["devops", "urgent"])
        self.assertEqual(notes[nid2]["tags"], ["research", "notes"])

    def test_add_custom_source(self):
        nid = memory.add("watch alert", source="watch")
        notes = {n["id"]: n for n in memory.list()}
        self.assertEqual(notes[nid]["source"], "watch")

    def test_delete(self):
        nid = memory.add("to be deleted")
        self.assertTrue(memory.delete(nid))
        self.assertEqual(memory.list(), [])
        self.assertFalse(memory.delete(nid))

    def test_persistence_across_loads(self):
        nid = memory.add("persisted note", tags=["x"])
        # fresh in-memory state is a new list() call reading from disk
        notes = memory.list()
        self.assertEqual([n["id"] for n in notes], [nid])
        raw = memory.notes_path().read_text(encoding="utf-8")
        self.assertIn("persisted note", raw)


class SearchTests(MemoryBase):
    def test_token_overlap_ranking(self):
        memory.add("the router is slow today")
        memory.add("backup ran at midnight")
        memory.add("the router backup schedule")
        hits = memory.search("router backup")
        # all three notes match at least one query token
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0]["text"], "the router backup schedule")
        self.assertEqual(hits[0]["score"], 2.0)
        self.assertEqual(hits[1]["score"], 1.0)
        self.assertEqual(hits[2]["score"], 1.0)

    def test_case_insensitive(self):
        memory.add("Python Refactor Notes")
        hits = memory.search("python refactor")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["score"], 2.0)

    def test_tag_match_bonus(self):
        # tagged note never mentions the query token — only the tag matches
        memory.add("rolled out to prod", tags=["deploy"])
        memory.add("deploy happened", tags=[])
        hits = memory.search("deploy")
        self.assertEqual(len(hits), 2)
        # tag hit (2.0) outranks the text-only hit (1.0)
        self.assertEqual(hits[0]["text"], "rolled out to prod")
        self.assertEqual(hits[0]["score"], 2.0)
        self.assertEqual(hits[1]["score"], 1.0)

    def test_no_match_returns_empty(self):
        memory.add("something completely different")
        self.assertEqual(memory.search("quantum entanglement"), [])

    def test_k_default_from_settings(self):
        for i in range(20):
            memory.add(f"note with token {i}")
        hits = memory.search("token")
        self.assertEqual(len(hits), 8)  # settings memory.k default
        # bump k via settings and search again
        from core import settings
        settings.set("memory.k", 3)
        hits = memory.search("token")
        self.assertEqual(len(hits), 3)

    def test_k_parameter_overrides(self):
        for i in range(10):
            memory.add(f"note with token {i}")
        hits = memory.search("token", k=2)
        self.assertEqual(len(hits), 2)

    def test_empty_query(self):
        memory.add("some note")
        self.assertEqual(memory.search(""), [])
        self.assertEqual(memory.search("   "), [])

    def test_hits_carry_id_and_score(self):
        nid = memory.add("unique marker phrase")
        hits = memory.search("marker")
        self.assertEqual(hits[0]["id"], nid)
        self.assertIn("score", hits[0])


class IndexTests(MemoryBase):
    def test_memory_index_maps_tokens_to_ids(self):
        a = memory.add("python bug fix")
        b = memory.add("python research notes", tags=["python"])
        idx = memory.MemoryIndex(memory.list())
        self.assertEqual(idx.tokens(), sorted(["python", "bug", "fix", "research", "notes"]))
        ids = idx.ids_for("python")
        self.assertEqual(len(ids), 2)
        self.assertIn(a, ids)
        self.assertIn(b, ids)

    def test_memory_index_handles_empty(self):
        idx = memory.MemoryIndex([])
        self.assertEqual(idx.tokens(), [])
        self.assertEqual(idx.ids_for("anything"), [])


class StatsTests(MemoryBase):
    def test_stats_counts_and_sources(self):
        memory.add("one", source="manual")
        memory.add("two", source="watch")
        memory.add("three", source="watch")
        s = memory.stats()
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["sources"], {"manual": 1, "watch": 2})
        self.assertIsNotNone(s["last_added"])


if __name__ == "__main__":
    unittest.main()
