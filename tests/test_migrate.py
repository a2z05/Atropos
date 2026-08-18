#!/usr/bin/env python3
"""v18 J+K tests — migration import (ask-first, revertible) + sealed guest
memory (guests know, cannot tell).

All hermetic via ATROPOS_HOME swap; the Hermes source store is a temp dir.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import detect, guest, migrate, settings  # noqa: E402


class _Hermetic(unittest.TestCase):
    _SNIP = ("HERMES_HOME", "ATROPOS_HOME")

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in self._SNIP}
        for k in self._SNIP:
            os.environ.pop(k, None)
        self.home = Path(tempfile.mkdtemp(prefix="atropos_mig_"))
        self.src = Path(tempfile.mkdtemp(prefix="atropos_src_"))
        os.environ["ATROPOS_HOME"] = str(self.home)
        self._u = settings.get
        import importlib
        import core.settings as _s
        importlib.reload(_s)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("ATROPOS_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def _src_config(self, text):
        (self.src / "config.yaml").write_text(text, encoding="utf-8")

    def _src_memory(self, notes):
        (self.src / "memory.json").write_text(
            json.dumps(notes, ensure_ascii=False), encoding="utf-8")

    def _src_skill(self, name, content):
        d = self.src / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(content, encoding="utf-8")


class PlanTests(_Hermetic):
    def test_plan_missing_source(self):
        p = migrate.import_plan(source=str(self.src) + "_nope")
        self.assertFalse(p["exists"])
        self.assertEqual(p["kinds"], {})

    def test_plan_detects_kinds(self):
        self._src_config("dashboard:\n  port: 9000\n")
        self._src_memory([{"text": "hello guest", "tags": []}])
        self._src_skill("demo", "---\nname: demo\ndescription: d\n---\n\nbody")
        p = migrate.import_plan(source=str(self.src))
        self.assertTrue(p["exists"])
        self.assertIn("config", p["kinds"])
        self.assertIn("memory", p["kinds"])
        self.assertIn("skills", p["kinds"])
        self.assertIn("dashboard.port", p["kinds"]["config"]["known_keys"])
        self.assertEqual(p["kinds"]["memory"]["notes"], 1)
        self.assertEqual(p["kinds"]["skills"]["skills"], ["demo"])

    def test_plan_never_writes(self):
        self._src_config("dashboard:\n  port: 9001\n")
        before = {p.name: p.stat().st_size for p in self.home.iterdir()} if self.home.exists() else {}
        migrate.import_plan(source=str(self.src))
        after = {p.name: p.stat().st_size for p in self.home.iterdir()} if self.home.exists() else {}
        self.assertEqual(before, after)


class ApplyTests(_Hermetic):
    def test_apply_requires_yes(self):
        self._src_config("dashboard:\n  port: 9002\n")
        r = migrate.import_apply(source=str(self.src))
        self.assertFalse(r["ok"])
        self.assertIn("yes", r["reason"])
        self.assertFalse((self.home / "config.yaml").exists())

    def test_apply_imports_config_and_persists(self):
        self._src_config("dashboard:\n  port: 9003\nskills:\n  auto_skill: true\n")
        r = migrate.import_apply(source=str(self.src), yes=True)
        self.assertTrue(r["ok"])
        self.assertIn("dashboard.port", r["imported"]["config"])
        self.assertIn("skills.auto_skill", r["imported"]["config"])
        self.assertEqual(settings.get("dashboard.port"), 9003)
        self.assertTrue(settings.get("skills.auto_skill"))

    def test_apply_memory_dedupes(self):
        self._src_memory([{"text": "a"}, {"text": "b"}])
        (self.home / "memory.json").write_text(
            json.dumps([{"text": "a"}]), encoding="utf-8")
        r = migrate.import_apply(source=str(self.src), yes=True, kinds=["memory"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["imported"]["memory"], 1)
        notes = json.loads((self.home / "memory.json").read_text(encoding="utf-8"))
        self.assertEqual([n["text"] for n in notes], ["a", "b"])

    def test_apply_skills_skips_existing_unless_replace(self):
        self._src_skill("demo", "---\nname: demo\ndescription: d\n---\n\nbody")
        self._src_skill("other", "---\nname: other\ndescription: d\n---\n\nbody")
        # pre-existing demo in target
        tgt = self.home / "skills" / "demo"
        tgt.mkdir(parents=True, exist_ok=True)
        (tgt / "SKILL.md").write_text("---\nname: demo\ndescription: keep\n---\n\nmine", encoding="utf-8")
        r = migrate.import_apply(source=str(self.src), yes=True, kinds=["skills"])
        self.assertEqual(r["imported"]["skills"], ["other"])
        # replace=True overwrites
        r2 = migrate.import_apply(source=str(self.src), yes=True, kinds=["skills"], replace=True)
        self.assertIn("demo", r2["imported"]["skills"])

    def test_apply_logs_migration(self):
        self._src_config("dashboard:\n  port: 9004\n")
        migrate.import_apply(source=str(self.src), yes=True)
        rows = migrate.history()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "import")
        self.assertIn("backup", rows[0])


class UndoTests(_Hermetic):
    def test_undo_restores_preimport_state(self):
        self._src_config("dashboard:\n  port: 9005\n")
        (self.home / "config.yaml").write_text("dashboard:\n  port: 1111\n", encoding="utf-8")
        migrate.import_apply(source=str(self.src), yes=True)
        self.assertEqual(settings.get("dashboard.port"), 9005)
        r = migrate.undo(yes=True)
        self.assertTrue(r["ok"])
        self.assertIn("config.yaml", r["restored"])
        text = (self.home / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("1111", text)

    def test_undo_requires_yes_and_no_history(self):
        r = migrate.undo()
        self.assertFalse(r["ok"])
        self.assertIn("yes", r["reason"])
        # with clean history
        (self.home / "migrations.jsonl").write_text("", encoding="utf-8")
        r = migrate.undo(yes=True)
        self.assertFalse(r["ok"])
        self.assertIn("no prior", r["reason"])

    def test_undo_removes_imported_skills(self):
        self._src_skill("demo", "---\nname: demo\ndescription: d\n---\n\nbody")
        migrate.import_apply(source=str(self.src), yes=True, kinds=["skills"])
        self.assertTrue((self.home / "skills" / "demo").exists())
        r = migrate.undo(yes=True)
        self.assertIn("demo", r["removed_skills"])
        self.assertFalse((self.home / "skills" / "demo").exists())


class SealedMemoryTests(_Hermetic):
    def test_guest_records_sealed_note(self):
        r = guest.record_sealed("222", "my secret plan")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)

    def test_owner_view_is_counts_only(self):
        guest.record_sealed("222", "secret-alpha")
        guest.record_sealed("222", "secret-beta")
        guest.record_sealed("333", "other-guest")
        view = guest.sealed_owner_view()
        by = {v["user"]: v["count"] for v in view}
        self.assertEqual(by.get("222"), 2)
        self.assertEqual(by.get("333"), 1)
        # content must not appear in the owner view
        text = json.dumps(view)
        self.assertNotIn("secret-alpha", text)
        self.assertNotIn("secret-beta", text)

    def test_guest_sees_only_own_notes(self):
        guest.record_sealed("222", "mine")
        guest.record_sealed("333", "theirs")
        visible = guest.sealed_visible("222")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["text"], "mine")

    def test_sealed_never_in_guest_memory_context(self):
        guest.record_sealed("222", "not for context")
        mem = guest.guest_memory(20)
        text = json.dumps(mem)
        self.assertNotIn("not for context", text)

    def test_sealed_persisted_across_loads(self):
        guest.record_sealed("222", "durable secret")
        # simulate reload — fresh read from disk
        reloaded = guest.sealed_memory()
        self.assertEqual(len(reloaded), 1)


if __name__ == "__main__":
    unittest.main()