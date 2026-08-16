#!/usr/bin/env python3
"""Capability probe tests — detection, section registry gating, fallback."""
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

from core import probe


class CapabilityBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_probe_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class ProbeTests(CapabilityBase):
    def test_all_harnesses_probed(self):
        caps = probe.probe_capabilities()
        for h in ("hermes", "claude", "omni", "ninerouter", "atropos"):
            self.assertIn(h, caps)
            self.assertIsInstance(caps[h], list)

    def test_atropos_builtins(self):
        caps = probe.probe_capabilities()
        for feat in ("dashboard", "backups", "agents", "middleware"):
            self.assertIn(feat, caps["atropos"])

    def test_hermes_absent_no_crash(self):
        with mock.patch.object(probe.detect, "hermes_home", return_value=Path(self.tmp) / "none"):
            caps = probe.probe_capabilities()
        self.assertIsInstance(caps["hermes"], list)


class RegistryTests(CapabilityBase):
    def test_missing_requirement_hides_section(self):
        shown = probe.available_sections({"sessions": "hermes:present",
                                          "middleware": None},
                                         caps={"hermes": [], "atropos": [], "claude": [],
                                               "omni": [], "ninerouter": []})
        self.assertNotIn("sessions", shown)
        self.assertIn("middleware", shown)  # never-gated

    def test_present_requirement_shows_section(self):
        shown = probe.available_sections({"sessions": "hermes:present"},
                                         caps={"hermes": ["present"], "atropos": [],
                                               "claude": [], "omni": [], "ninerouter": []})
        self.assertIn("sessions", shown)

    def test_unknown_capability_never_crashes(self):
        caps = {"hermes": ["brand-new-feature"], "claude": [], "atropos": [],
                "omni": [], "ninerouter": []}
        shown = probe.available_sections(probe.SECTION_REQUIRES, caps)
        self.assertIsInstance(shown, list)


class ApiTests(CapabilityBase):
    def test_returns_shape(self):
        from core import dashboard
        d = dashboard._api_capabilities()
        self.assertTrue(d["ok"])
        self.assertIn("capabilities", d)
        self.assertIn("sections", d)


if __name__ == "__main__":
    unittest.main()
