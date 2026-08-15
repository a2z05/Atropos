#!/usr/bin/env python3
"""Atropos commands tests — universal commands & aliases CRUD + validation."""

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

from core import commands  # noqa: E402
from core import detect  # noqa: E402


class CommandsBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_commands_")
        self.home = Path(self.tmp)
        os.environ["ATROPOS_HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.home / ".hermes")
        self._home_patch = mock.patch.object(
            detect, "_home", return_value=self.home)
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def store(self) -> dict:
        p = self.home / "commands.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))


class CommandTests(CommandsBase):
    def test_add_and_list(self):
        commands.add_command("docs", "Write API docs for {topic}",
                             "Generate documentation")
        commands.add_command("sum", "Summarize {topic}")
        entries = commands.list_commands()
        names = [c["name"] for c in entries]
        self.assertEqual(names, ["docs", "sum"])  # sorted
        docs = entries[0]
        self.assertEqual(docs["template"], "Write API docs for {topic}")
        self.assertEqual(docs["description"], "Generate documentation")
        self.assertEqual(docs["mode"], "atropos")  # default mode

        data = commands.list()
        self.assertEqual([c["name"] for c in data["commands"]], names)
        self.assertEqual(data["aliases"], {})

    def test_add_with_mode_and_persistence(self):
        commands.add_command("deploy", "Deploy to {env}", mode="claude")
        entry = commands.list_commands()[0]
        self.assertEqual(entry["mode"], "claude")
        # persisted to ~/.atropos/commands.json
        raw = self.store()
        self.assertEqual(raw["commands"][0]["name"], "deploy")

    def test_add_invalid_names_rejected(self):
        for bad in ("..", "../evil", "a/b", "a b", "a.b", "", "x" * 33,
                    "-lead", "_lead", "with space"):
            with self.assertRaises(ValueError, msg=f"name {bad!r} accepted"):
                commands.add_command(bad, "template")
        self.assertEqual(self.store(), {})

    def test_add_empty_template_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError, msg=f"template {bad!r} accepted"):
                commands.add_command("ok-name", bad)
        self.assertEqual(self.store(), {})

    def test_add_duplicate_rejected(self):
        commands.add_command("docs", "t1")
        with self.assertRaises(ValueError):
            commands.add_command("docs", "t2")
        self.assertEqual(len(commands.list_commands()), 1)

    def test_remove_command(self):
        commands.add_command("docs", "t")
        commands.add_alias("d", "docs")
        res = commands.remove_command("docs")
        self.assertTrue(res["ok"])
        self.assertEqual(commands.list_commands(), [])
        # dangling aliases are cleaned up
        self.assertEqual(commands.list_aliases(), {})
        with self.assertRaises(FileNotFoundError):
            commands.remove_command("docs")

    def test_remove_command_invalid_name(self):
        with self.assertRaises(ValueError):
            commands.remove_command("../evil")


class AliasTests(CommandsBase):
    def test_add_resolve_remove_alias(self):
        commands.add_command("docs", "t")
        res = commands.add_alias("d", "docs")
        self.assertTrue(res["ok"])
        self.assertEqual(res["resolves_to"], "docs")
        self.assertEqual(commands.resolve_alias("d"), "docs")
        self.assertEqual(commands.resolve_alias("docs"), "docs")
        self.assertIsNone(commands.resolve_alias("ghost"))
        self.assertEqual(commands.list_aliases(), {"d": "docs"})

        commands.remove_alias("d")
        self.assertEqual(commands.list_aliases(), {})
        self.assertIsNone(commands.resolve_alias("d"))
        with self.assertRaises(FileNotFoundError):
            commands.remove_alias("d")

    def test_alias_chain_and_loop(self):
        commands.add_command("docs", "t")
        commands.add_alias("d", "docs")
        commands.add_alias("dd", "d")  # chain dd -> d -> docs
        self.assertEqual(commands.resolve_alias("dd"), "docs")
        # a self-referential alias resolves to nothing
        commands.add_alias("loop1", "docs")
        commands.add_alias("loop2", "loop1")
        # rewrite loop1 to point at loop2 -> cycle
        commands.add_alias("loop1", "loop2")
        self.assertIsNone(commands.resolve_alias("loop1"))

    def test_alias_redefinition_updates(self):
        commands.add_command("a", "t")
        commands.add_command("b", "t")
        commands.add_alias("x", "a")
        commands.add_alias("x", "b")  # re-alias
        self.assertEqual(commands.resolve_alias("x"), "b")
        self.assertEqual(commands.list_aliases(), {"x": "b"})

    def test_alias_validation(self):
        commands.add_command("docs", "t")
        for bad in ("..", "../x", "a/b", "a b", "a.b", "", "x" * 33):
            with self.assertRaises(ValueError, msg=f"alias {bad!r} accepted"):
                commands.add_alias(bad, "docs")
            with self.assertRaises(ValueError, msg=f"target {bad!r} accepted"):
                commands.add_alias("ok-alias", bad)
        # unknown target rejected
        with self.assertRaises(ValueError):
            commands.add_alias("d", "ghost")
        # self-alias rejected
        with self.assertRaises(ValueError):
            commands.add_alias("d", "d")
        # no rejected call persisted anything
        self.assertEqual(self.store()["aliases"], {})
        self.assertEqual(commands.resolve_alias("d"), None)

    def test_remove_command_drops_aliases(self):
        commands.add_command("docs", "t")
        commands.add_command("other", "t")
        commands.add_alias("d", "docs")
        commands.add_alias("o", "other")
        commands.remove_command("docs")
        self.assertIsNone(commands.resolve_alias("d"))
        self.assertEqual(commands.resolve_alias("o"), "other")


class StatsTests(CommandsBase):
    def test_stats(self):
        self.assertEqual(commands.stats(),
                         {"commands": 0, "aliases": 0, "modes": {}})
        commands.add_command("a", "t1")
        commands.add_command("b", "t2", mode="claude")
        commands.add_alias("x", "a")
        s = commands.stats()
        self.assertEqual(s["commands"], 2)
        self.assertEqual(s["aliases"], 1)
        self.assertEqual(s["modes"], {"atropos": 1, "claude": 1})

    def test_corrupt_store_recovers(self):
        p = self.home / "commands.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(commands.list_commands(), [])
        commands.add_command("docs", "t")  # recovers and rewrites
        self.assertEqual(commands.resolve_alias("nope"), None)


if __name__ == "__main__":
    unittest.main()
