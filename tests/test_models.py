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
        self.assertEqual(names, ["llama3"])
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["llama3"]["provider"], "local")
        self.assertEqual(by_name["llama3"]["model"], "llama3")
        self.assertEqual(by_name["llama3"]["base_url"], "http://localhost:11434/v1")
        self.assertEqual(by_name["llama3"]["api_key_env"], "OLLAMA_HOST")
        # seed entries are enabled and shared by default
        for e in entries:
            self.assertTrue(e["enabled"])
            self.assertEqual(e["mode"], "shared")

    def test_seed_written_on_demand(self):
        # read-only access never creates the file; the first write persists
        # the seed so it is not lost on the next read
        self.assertFalse((self.home / "models.json").exists())
        models.assign("hermes", "llama3")
        p = self.home / "models.json"
        self.assertTrue(p.exists())
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in data["entries"]],
                         ["llama3"])


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
        models.add("mixtral", provider="local", model="mixtral-8x7b")
        with self.assertRaises(ValueError):
            models.add("mixtral")

    def test_add_invalid_name(self):
        with self.assertRaises(ValueError):
            models.add("../evil")
        with self.assertRaises(ValueError):
            models.add("bad/name")

    def test_enable_disable(self):
        models.add("mixtral", provider="local", model="mixtral-8x7b")
        models.disable("llama3")
        self.assertFalse(self.store()["entries"][0]["enabled"])
        models.enable("llama3")
        self.assertTrue(self.store()["entries"][0]["enabled"])
        models.disable("mixtral")
        self.assertFalse(self.store()["entries"][-1]["enabled"])

    def test_remove_clears_assignments(self):
        models.add("gpt-4o", provider="openai", model="gpt-4o",
                   base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY")
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
        models.add("gpt-4o", provider="openai", model="gpt-4o",
                   base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY")
        models.assign("hermes", "gpt-4o")
        self.assertEqual(models.assignments(), {"hermes": "gpt-4o"})
        a = models.active("hermes")
        self.assertEqual(a["name"], "gpt-4o")
        self.assertEqual(a["model"], "gpt-4o")
        self.assertEqual(a["base_url"], "https://api.openai.com/v1")
        self.assertEqual(a["api_key_env"], "OPENAI_API_KEY")

    def test_active_fallback_to_first_enabled(self):
        # no assignment → first enabled entry (llama3)
        a = models.active("hermes")
        self.assertEqual(a["name"], "llama3")
        self.assertEqual(a["model"], "llama3")
        self.assertEqual(a["api_key_env"], "OLLAMA_HOST")

    def test_active_skips_disabled_assignment(self):
        models.add("mixtral", provider="local", model="mixtral-8x7b")
        models.assign("hermes", "mixtral")
        models.disable("mixtral")
        a = models.active("hermes")
        self.assertNotEqual(a["name"], "mixtral")  # falls back
        self.assertEqual(a["name"], "llama3")

    def test_active_unknown_harness(self):
        self.assertIsNone(models.active("openai"))

    def test_assign_validation(self):
        with self.assertRaises(ValueError):
            models.assign("openai", "llama3")
        with self.assertRaises(FileNotFoundError):
            models.assign("hermes", "ghost")


if __name__ == "__main__":
    unittest.main()
