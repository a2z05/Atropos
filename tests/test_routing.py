#!/usr/bin/env python3
"""Atropos task routing hub tests — defaults, aliases, heuristics, persistence."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import routing, settings  # noqa: E402


class RoutingBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_routing_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class DefaultsTests(RoutingBase):
    def test_core_harnesses(self):
        self.assertEqual(routing.CORE_HARNESSES,
                         ("clotho", "lachesis", "atropos", "auto"))

    def test_default_categories(self):
        self.assertEqual(routing.DEFAULT_CATEGORIES["coding"], "lachesis")
        self.assertEqual(routing.DEFAULT_CATEGORIES["debugging"], "lachesis")
        self.assertEqual(routing.DEFAULT_CATEGORIES["devops"], "lachesis")
        self.assertEqual(routing.DEFAULT_CATEGORIES["mlops"], "lachesis")
        self.assertEqual(routing.DEFAULT_CATEGORIES["research"], "clotho")
        self.assertEqual(routing.DEFAULT_CATEGORIES["summaries"], "clotho")
        self.assertEqual(routing.DEFAULT_CATEGORIES["email"], "clotho")
        self.assertEqual(routing.DEFAULT_CATEGORIES["media"], "clotho")
        self.assertEqual(routing.DEFAULT_CATEGORIES["monitoring"], "atropos")
        self.assertEqual(routing.DEFAULT_CATEGORIES["general"], "auto")

    def test_categories_ordered(self):
        cats = routing.categories()
        self.assertEqual(cats, list(routing.DEFAULT_CATEGORIES.keys()))
        self.assertIn("coding", cats)
        self.assertIn("general", cats)


class AliasTests(RoutingBase):
    def test_normalize_aliases(self):
        self.assertEqual(routing.normalize("hermes"), "clotho")
        self.assertEqual(routing.normalize("claude"), "lachesis")
        self.assertEqual(routing.normalize("internal"), "atropos")
        self.assertEqual(routing.normalize("HERMES"), "clotho")
        self.assertEqual(routing.normalize("Auto"), "auto")
        for h in routing.CORE_HARNESSES:
            self.assertEqual(routing.normalize(h), h)

    def test_normalize_rejects_unknown(self):
        for bad in ("openai", "gpt", "", "claude code", "deepmo"):
            with self.assertRaises(ValueError):
                routing.normalize(bad)

    def test_get_defaults_by_category(self):
        self.assertEqual(routing.get("coding"), "lachesis")
        self.assertEqual(routing.get("research"), "clotho")
        self.assertEqual(routing.get("monitoring"), "atropos")

    def test_get_default_fallback(self):
        self.assertEqual(routing.get("general"), routing._fallback())
        self.assertIn(routing.get("general"), ("clotho", "lachesis", "atropos"))


class DispatchTests(RoutingBase):
    def test_dispatch_override(self):
        routing.set("coding", "clotho")
        r = routing.dispatch("fix the python bug in main.py")
        self.assertEqual(r["category"], "debugging")
        # override is stored per-category: coding→clotho doesn't touch debugging
        self.assertEqual(r["harness"], "lachesis")
        self.assertEqual(r["by"], "default")

    def test_dispatch_override_alias_normalized(self):
        routing.set("coding", "hermes")
        r = routing.dispatch("refactor the api handler")
        self.assertEqual(r["category"], "coding")
        self.assertEqual(r["harness"], "clotho")
        self.assertEqual(r["by"], "override")
        self.assertEqual(routing._map()["coding"], "clotho")

    def test_dispatch_debugging_phrase(self):
        r = routing.dispatch("fix the python bug in main.py")
        self.assertEqual(r["category"], "debugging")
        self.assertEqual(r["harness"], "lachesis")
        self.assertEqual(r["by"], "default")
        self.assertGreaterEqual(r["score"]["lachesis"], 1)

    def test_dispatch_summarize_phrase(self):
        r = routing.dispatch("summarize the logs")
        self.assertEqual(r["category"], "summaries")
        self.assertEqual(r["harness"], "clotho")
        self.assertEqual(r["by"], "default")

    def test_dispatch_health_phrase(self):
        r = routing.dispatch("check disk health")
        self.assertEqual(r["category"], "monitoring")
        self.assertEqual(r["harness"], "atropos")
        self.assertEqual(r["by"], "default")

    def test_dispatch_auto_heuristic_lachesis(self):
        r = routing.dispatch("fix the python bug in main.py")
        self.assertEqual(r["by"], "default")
        self.assertEqual(r["harness"], "lachesis")

    def test_dispatch_auto_heuristic_clotho(self):
        r = routing.dispatch("summarize the logs")
        self.assertEqual(r["by"], "default")
        self.assertEqual(r["harness"], "clotho")

    def test_dispatch_auto_heuristic_atropos(self):
        r = routing.dispatch("check disk health")
        self.assertEqual(r["by"], "default")
        self.assertEqual(r["harness"], "atropos")

    def test_dispatch_general_unmatched(self):
        r = routing.dispatch("what is the weather like in tokyo")
        self.assertEqual(r["category"], "general")
        self.assertIn(r["by"], ("default", "heuristic"))
        self.assertIn(r["harness"], ("clotho", "lachesis", "atropos"))

    def test_dispatch_auto_override_heuristic(self):
        routing.set("coding", "auto")
        r = routing.dispatch("write a python script to parse logs")
        self.assertEqual(r["by"], "heuristic")
        self.assertEqual(r["harness"], "lachesis")
        # scoring is present
        self.assertGreaterEqual(r["score"]["lachesis"], 1)


class SetValidationTests(RoutingBase):
    def test_bad_harness_rejected(self):
        with self.assertRaises(ValueError):
            routing.set("coding", "deepseek")
        with self.assertRaises(ValueError):
            routing.set("coding", "gpt4")

    def test_bad_category_rejected(self):
        with self.assertRaises(ValueError):
            routing.set("bad category!", "lachesis")
        with self.assertRaises(ValueError):
            routing.set("1starting-with-digit", "lachesis")
        with self.assertRaises(ValueError):
            routing.set("", "lachesis")

    def test_valid_custom_category(self):
        routing.set("my-category", "clotho")
        self.assertEqual(routing._map()["my-category"], "clotho")

    def test_set_auto_accepted(self):
        routing.set("coding", "auto")
        self.assertEqual(routing._map()["coding"], "auto")


class CustomCategoryTests(RoutingBase):
    def test_add_custom_category(self):
        routing.add("infosec", "lachesis")
        self.assertIn("infosec", routing.categories())
        self.assertEqual(routing.get("infosec"), "lachesis")
        self.assertEqual(routing.dispatch("infosec")["harness"], "lachesis")

    def test_add_accepts_kwargs(self):
        routing.add("finops", "clotho", description="finance ops", keywords=["billing"])
        self.assertEqual(routing.get("finops"), "clotho")

    def test_remove_custom_category(self):
        routing.add("infosec", "lachesis")
        self.assertTrue(routing.remove("infosec"))
        self.assertNotIn("infosec", routing.categories())
        self.assertFalse(routing.remove("infosec"))

    def test_remove_override_of_builtin(self):
        routing.set("coding", "clotho")
        self.assertTrue(routing.remove("coding"))
        self.assertEqual(routing.get("coding"), "lachesis")  # back to default


class PersistenceTests(RoutingBase):
    def test_map_persists_roundtrip(self):
        routing.set("coding", "hermes")
        routing.set("research", "lachesis")
        saved = settings.get("routing.map")
        self.assertEqual(saved["coding"], "clotho")
        self.assertEqual(saved["research"], "lachesis")
        # fresh settings read (new settings.load) sees the same map
        self.assertEqual(routing._map(), saved)

    def test_merged_map_keeps_existing_keys(self):
        routing.set("coding", "clotho")
        routing.set("coding", "auto")
        m = routing._map()
        self.assertEqual(m["coding"], "auto")

    def test_list_config(self):
        routing.set("coding", "claude")
        cfg = routing.list_config()
        self.assertEqual(cfg["map"]["coding"], "lachesis")
        self.assertEqual(cfg["default"], "auto")
        self.assertIs(cfg["enabled"], True)

    def test_default_setting_roundtrip(self):
        settings.set("routing.default", "lachesis")
        self.assertEqual(settings.get("routing.default"), "lachesis")
        cfg = routing.list_config()
        self.assertEqual(cfg["default"], "lachesis")
        # general category now resolves to the configured default
        r = routing.dispatch("nothing matched here at all")
        self.assertEqual(r["by"], "default")
        self.assertEqual(r["harness"], "lachesis")


if __name__ == "__main__":
    unittest.main()
