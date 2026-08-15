#!/usr/bin/env python3
"""Atropos models tests — seed, CRUD, per-harness assignment resolution."""

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

from core import models  # noqa: E402


class ModelsBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_models_")
        self.home = Path(self.tmp)
        os.environ["ATROPOS_HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.home / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def store(self) -> dict:
        return json.loads((self.home / "models.json").read_text(encoding="utf-8"))


class SeedTests(ModelsBase):
    def test_default_seed(self):
        entries = models.list_models()
        names = [e["name"] for e in entries]
        self.assertEqual(names, ["deepmo", "gpt-4o", "llama3"])
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["deepmo"]["provider"], "nain")
        self.assertEqual(by_name["deepmo"]["model"], "deepmo")
        self.assertEqual(by_name["gpt-4o"]["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(by_name["llama3"]["api_key_env"], "OLLAMA_HOST")
        # seed entries are enabled and shared by default
        for e in entries:
            self.assertTrue(e["enabled"])
            self.assertEqual(e["mode"], "shared")

    def test_seed_written_on_demand(self):
        models.list_models()
        self.assertTrue((self.home / "models.json").exists())


class RegistryTests(ModelsBase):
    def test_add(self):
        models.add("mixtral", provider="local", model="mixtral-8x7b")
        e = self.store()["entries"][-1]
        self.assertEqual(e["name"], "mixtral")
        self.assertEqual(e["provider"], "local")
        self.assertEqual(e["model"], "mixtral-8x7b")
        # inherits provider defaults from router.ROUTERS
        self.assertEqual(e["base_url"], "http://localhost:11434/v1")
        self.assertEqual(e["api_key_env"], "OLLAMA_HOST")

    def test_add_custom_provider(self):
        models.add("custom", provider="acme", model="acme-1",
                   base_url="https://acme.example/v1", api_key_env="ACME_KEY")
        e = self.store()["entries"][-1]
        self.assertEqual(e["base_url"], "https://acme.example/v1")
        self.assertEqual(e["api_key_env"], "ACME_KEY")

    def test_add_duplicate_raises(self):
        with self.assertRaises(ValueError):
            models.add("deepmo")

    def test_add_invalid_name(self):
        with self.assertRaises(ValueError):
            models.add("../evil")
        with self.assertRaises(ValueError):
            models.add("bad/name")

    def test_enable_disable(self):
        models.disable("deepmo")
        self.assertFalse(self.store()["entries"][0]["enabled"])
        models.enable("deepmo")
        self.assertTrue(self.store()["entries"][0]["enabled"])

    def test_remove_clears_assignments(self):
        models.assign("hermes", "gpt-4o")
        models.remove("gpt-4o")
        self.assertEqual(models.assignments(), {})
        with self.assertRaises(FileNotFoundError):
            models.remove("gpt-4o")

    def test_remove_unknown_raises(self):
        with self.assertRaises(FileNotFoundError):
            models.remove("ghost")


class AssignmentTests(ModelsBase):
    def test_assign_and_active(self):
        models.assign("hermes", "gpt-4o")
        self.assertEqual(models.assignments(), {"hermes": "gpt-4o"})
        a = models.active("hermes")
        self.assertEqual(a["name"], "gpt-4o")
        self.assertEqual(a["model"], "gpt-4o")
        self.assertEqual(a["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(a["api_key_env"], "OPENAI_API_KEY")

    def test_active_fallback_to_first_enabled(self):
        # no assignment → first enabled entry (deepmo)
        a = models.active("hermes")
        self.assertEqual(a["name"], "deepmo")
        self.assertEqual(a["model"], "deepmo")
        self.assertEqual(a["api_key_env"], "OPENAI_API_KEY")

    def test_active_skips_disabled_assignment(self):
        models.assign("hermes", "gpt-4o")
        models.disable("gpt-4o")
        a = models.active("hermes")
        self.assertNotEqual(a["name"], "gpt-4o")  # falls back
        self.assertEqual(a["name"], "deepmo")

    def test_active_unknown_harness(self):
        self.assertIsNone(models.active("openai"))

    def test_assign_validation(self):
        with self.assertRaises(ValueError):
            models.assign("openai", "deepmo")
        with self.assertRaises(ValueError):
            models.assign("hermes", "ghost")


if __name__ == "__main__":
    unittest.main()
