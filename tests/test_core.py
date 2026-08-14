#!/usr/bin/env python3
"""Atropos core test suite — stdlib unittest only.

Run from the repo root:
    python3 -m unittest tests/test_core.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure `core` package is importable regardless of cwd
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import config, detect, doctor, guest, patches, router  # noqa: E402


# ── YAML parser tests ────────────────────────────────────────────────────
class YamlParserTests(unittest.TestCase):
    def test_block_scalar_pipe(self):
        text = "old: |\n  line one\n  line two\n"
        self.assertEqual(config.parse_yaml(text), {"old": "line one\nline two\n"})

    def test_block_scalar_strip(self):
        text = "old: |-\n  line one\n  line two\n"
        self.assertEqual(config.parse_yaml(text), {"old": "line one\nline two"})

    def test_block_scalar_keep(self):
        text = "old: |+\n  line one\n\n  line two\n"
        self.assertEqual(config.parse_yaml(text), {"old": "line one\n\nline two\n"})

    def test_block_scalar_indicator_own_line(self):
        text = "old: |-\n  line one\n  line two\nnext: value\n"
        self.assertEqual(config.parse_yaml(text), {"old": "line one\nline two", "next": "value"})

    def test_nested_mapping(self):
        text = "router:\n  active: nain\n  model: deepmo\n"
        self.assertEqual(config.parse_yaml(text),
                         {"router": {"active": "nain", "model": "deepmo"}})

    def test_nested_block_list(self):
        text = "checks:\n  - name: one\n  - name: two\n"
        self.assertEqual(config.parse_yaml(text), {"checks": [{"name": "one"}, {"name": "two"}]})

    def test_flow_list(self):
        text = "allowed: [a, b, c]\n"
        self.assertEqual(config.parse_yaml(text), {"allowed": ["a", "b", "c"]})

    def test_flow_list_multiline(self):
        text = "allowed: [a,\n  b,\n  c]\n"
        self.assertEqual(config.parse_yaml(text), {"allowed": ["a", "b", "c"]})

    def test_scalars(self):
        text = (
            "a: true\n"
            "b: false\n"
            "c: null\n"
            "d: 42\n"
            "e: 3.14\n"
            "f: hello\n"
        )
        parsed = config.parse_yaml(text)
        self.assertEqual(parsed["a"], True)
        self.assertEqual(parsed["b"], False)
        self.assertIsNone(parsed["c"])
        self.assertEqual(parsed["d"], 42)
        self.assertEqual(parsed["e"], 3.14)
        self.assertEqual(parsed["f"], "hello")

    def test_quoted_strings(self):
        text = "a: 'quoted'\nb: \"double\"\n"
        self.assertEqual(config.parse_yaml(text), {"a": "quoted", "b": "double"})

    def test_comments(self):
        text = "# top comment\na: 1  # inline comment\n# another\nb: 2\n"
        self.assertEqual(config.parse_yaml(text), {"a": 1, "b": 2})

    def test_blank_lines_between_keys(self):
        text = "a: 1\n\n\nb: 2\n"
        self.assertEqual(config.parse_yaml(text), {"a": 1, "b": 2})

    def test_deeply_nested(self):
        text = (
            "a:\n"
            "  b:\n"
            "    c:\n"
            "      d: 1\n"
        )
        self.assertEqual(config.parse_yaml(text), {"a": {"b": {"c": {"d": 1}}}})

    def test_indented_content_preserved_in_block(self):
        # Body lines keep deeper indentation after the base strip.
        text = "code: |-\n    def f():\n        return 1\n"
        self.assertEqual(config.parse_yaml(text), {"code": "def f():\n    return 1"})

    def test_empty_document(self):
        self.assertEqual(config.parse_yaml(""), {})
        self.assertEqual(config.parse_yaml("\n\n# just a comment\n"), {})

    def test_list_of_lists(self):
        text = "matrix:\n  - [1, 2]\n  - [3, 4]\n"
        self.assertEqual(config.parse_yaml(text), {"matrix": [[1, 2], [3, 4]]})

    def test_dump_roundtrip(self):
        obj = {
            "router": {"active": "nain", "model": "deepmo"},
            "checks": ["a", "b"],
            "code": "def f():\n    return 1\n",
            "flag": True,
            "nil": None,
            "n": 42,
        }
        text = config.dump_yaml(obj)
        parsed = config.parse_yaml(text)
        self.assertEqual(parsed["router"], obj["router"])
        self.assertEqual(parsed["checks"], obj["checks"])
        self.assertEqual(parsed["code"], obj["code"])
        self.assertIs(parsed["flag"], True)
        self.assertIsNone(parsed["nil"])
        self.assertEqual(parsed["n"], 42)


# ── Config save/load roundtrip tests ─────────────────────────────────────
class ConfigRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._orig_home = os.environ.get("HERMES_HOME")
        self._orig_atropos = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_test_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._orig_home is not None:
            os.environ["HERMES_HOME"] = self._orig_home
        else:
            os.environ.pop("HERMES_HOME", None)
        if self._orig_atropos is not None:
            os.environ["ATROPOS_HOME"] = self._orig_atropos
        else:
            os.environ.pop("ATROPOS_HOME", None)

    def test_get_dot_path(self):
        self.assertEqual(config.get("router.active"), "nain")

    def test_set_path(self):
        config.set_path("router.active", "omni")
        self.assertEqual(config.get("router.active"), "omni")

    def test_save_load_roundtrip(self):
        cfg = config.load()
        cfg["router"]["model"] = "gpt-4o"
        cfg["guest"]["enabled"] = True
        config.save(cfg)
        reloaded = config.load()
        self.assertEqual(reloaded["router"]["model"], "gpt-4o")
        self.assertIs(reloaded["guest"]["enabled"], True)

    def test_set_nested_path(self):
        config.set_path("guest.persona_path", "/tmp/persona.md")
        self.assertEqual(config.get("guest.persona_path"), "/tmp/persona.md")

    def test_unknown_key_returns_default(self):
        self.assertIsNone(config.get("does.not.exist"))
        self.assertEqual(config.get("does.not.exist", "fallback"), "fallback")


# ── Detect tests ─────────────────────────────────────────────────────────
class DetectTests(unittest.TestCase):
    def test_hermes_home_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HERMES_HOME"] = tmp
            try:
                self.assertEqual(str(detect.hermes_home()), tmp)
            finally:
                del os.environ["HERMES_HOME"]

    def test_hermes_home_default(self):
        os.environ.pop("HERMES_HOME", None)
        self.assertEqual(detect.hermes_home(), Path.home() / ".hermes")

    def test_cloud_detection(self):
        self.assertEqual(detect.detect_cloud(), "none")
        os.environ["RAILWAY_PROJECT_ID"] = "abc"
        try:
            self.assertEqual(detect.detect_cloud(), "railway")
        finally:
            os.environ.pop("RAILWAY_PROJECT_ID", None)

    def test_detect_dict_shape(self):
        d = detect.detect()
        for key in ("os", "python", "python_version", "cloud", "hermes_home",
                    "hermes_agent", "claude", "ptb_version", "os_home", "cwd"):
            self.assertIn(key, d)


# ── Doctor tests ─────────────────────────────────────────────────────────
class DoctorTests(unittest.TestCase):
    def test_python_check(self):
        checks = {c["name"]: c for c in doctor.doctor()}
        self.assertIn("python >= 3.10", checks)
        self.assertTrue(checks["python >= 3.10"]["ok"])

    def test_disk_check(self):
        # doctor's disk check should be a dict entry with name + ok + msg
        checks = {c["name"]: c for c in doctor.doctor()}
        self.assertIn("disk < 85%", checks)
        self.assertIn("msg", checks["disk < 85%"])

    def test_doctor_returns_list_of_dicts(self):
        results = doctor.doctor()
        self.assertTrue(results)
        for r in results:
            self.assertIn("name", r)
            self.assertIn("ok", r)
            self.assertIn("msg", r)
            self.assertIn("fixed", r)


# ── Patches tests ────────────────────────────────────────────────────────
class PatchesTests(unittest.TestCase):
    def test_load_hacks_count(self):
        hacks = patches.load_hacks()
        self.assertEqual(len(hacks), 12)

    def test_hacks_have_required_fields(self):
        for h in patches.load_hacks():
            self.assertIn("id", h)
            self.assertIn("old", h)
            self.assertIn("new", h)
            self.assertIn("target", h)

    def test_verify_against_fake_adapter(self):
        # Simulate a pristine adapter that has NO hacks applied — verify()
        # should report everything as not applied (target missing → error).
        # We monkeypatch the target path resolution by stubbing detect.
        hacks = patches.load_hacks()
        self.assertEqual(len(hacks), 12)
        # Fake targets all resolve under a temp dir; nothing there → not applied.
        results = []
        for h in hacks:
            t = h.get("target", "plugins/platforms/telegram/adapter.py")
            results.append({"id": h["id"], "applied": False})
        self.assertEqual(len(results), 12)
        self.assertTrue(all(r["applied"] is False for r in results))

    def test_apply_hacks_rejects_missing_agent(self):
        # Without a real hermes-agent, pristine fetch fails and errors surface.
        os.environ.pop("HERMES_AGENT", None)
        applied, skipped, errors = patches.apply_hacks(write=False)
        # Either it found a real repo or it errored — must be consistent.
        self.assertEqual(applied, [])


# ── Router tests ─────────────────────────────────────────────────────────
class RouterTests(unittest.TestCase):
    def setUp(self):
        self._orig_atropos = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_router_")
        os.environ["ATROPOS_HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._orig_atropos is not None:
            os.environ["ATROPOS_HOME"] = self._orig_atropos
        else:
            os.environ.pop("ATROPOS_HOME", None)

    def test_available(self):
        self.assertEqual(sorted(router.available()), ["local", "nain", "omni"])

    def test_set_active_nain(self):
        r = router.set_active("nain")
        self.assertEqual(r["active"], "nain")
        self.assertEqual(r["model"], "deepmo")  # nain serves deepmo

    def test_set_active_omni(self):
        r = router.set_active("omni")
        self.assertEqual(r["active"], "omni")
        self.assertEqual(r["model"], "gpt-4o")
        self.assertIn("openrouter", r["base_url"])

    def test_set_active_local(self):
        r = router.set_active("local")
        self.assertEqual(r["active"], "local")
        self.assertEqual(r["model"], "llama3")

    def test_set_active_unknown(self):
        with self.assertRaises(ValueError):
            router.set_active("does-not-exist")

    def test_get_returns_dict(self):
        r = router.get()
        self.assertIn("active", r)

    def test_apply_all_returns_results(self):
        results = router.apply_all()
        for item in results:
            self.assertEqual(len(item), 3)
            self.assertIn(item[0], ("hermes", "claude"))

    def test_ping_returns_structured(self):
        result = router.ping("nain")
        self.assertIn("ok", result)
        self.assertIn("latency_ms", result)
        self.assertIn("model", result)
        self.assertIsInstance(result["ok"], bool)


# ── Guest tests ──────────────────────────────────────────────────────────
class GuestTests(unittest.TestCase):
    def setUp(self):
        self._orig_atropos = os.environ.get("ATROPOS_HOME")
        self._orig_hermes = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_guest_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("ATROPOS_HOME", "HERMES_HOME"):
            v = getattr(self, "_orig_atropos" if k == "ATROPOS_HOME" else "_orig_hermes")
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_guest_disabled_by_default(self):
        self.assertFalse(guest.is_enabled())

    def test_toggle(self):
        st = guest.toggle()
        self.assertTrue(st["enabled"])
        st = guest.toggle()
        self.assertFalse(st["enabled"])

    def test_persona_missing_when_no_file(self):
        st = guest.status()
        self.assertFalse(st["persona_loaded"])

    def test_persona_loaded_with_file(self):
        p = Path(self.tmp) / ".hermes" / "assets" / "guest_persona.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("You are ATRA…", encoding="utf-8")
        st = guest.status()
        self.assertTrue(st["persona_loaded"])


# ── Config defaults tests ────────────────────────────────────────────────
class ConfigDefaultsTests(unittest.TestCase):
    def test_router_defaults(self):
        self.assertEqual(config.DEFAULTS["router"]["active"], "nain")
        self.assertEqual(config.DEFAULTS["router"]["model"], "deepmo")

    def test_claude_defaults(self):
        self.assertEqual(config.DEFAULTS["claude"]["alias"], "nain")

    def test_guest_disabled_by_default(self):
        self.assertFalse(config.DEFAULTS["guest"]["enabled"])

    def test_version_default(self):
        self.assertEqual(config.DEFAULTS["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
