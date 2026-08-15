#!/usr/bin/env python3
"""Atropos failover tests — router auto-failover behavior."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import failover, router, settings  # noqa: E402


class FailoverBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_failover_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        self._orig_ping = router.ping
        self._orig_set = router.set_active
        settings.set("failover.enabled", True)
        settings.set("failover.retries", 2)
        settings.set("failover.order", ["nain", "omni", "local"])

    def tearDown(self):
        router.ping = self._orig_ping
        router.set_active = self._orig_set
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class FailoverTests(FailoverBase):
    def test_healthy_router_no_switch(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": True, "latency_ms": 42, "model": name, "error": None}
        r = failover.check_now()
        self.assertTrue(r["ok"])
        self.assertEqual(failover.load_state()["active"], "nain")
        self.assertEqual(failover.load_state()["failures"], 0)

    def test_switches_after_retries(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": False, "latency_ms": 50, "model": name, "error": "down"}
        settings.set("failover.retries", 2)
        # first check: 1 failure
        r1 = failover.check_now()
        self.assertEqual(r1["state"]["failures"], 1)
        self.assertEqual(r1["state"]["active"], "nain")
        # second check: switch to omni
        r2 = failover.check_now()
        self.assertEqual(r2["switched"]["to"], "omni")
        self.assertEqual(failover.load_state()["active"], "omni")

    def test_state_persisted(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": False, "latency_ms": 1, "model": name, "error": "down"}
        settings.set("failover.retries", 1)
        failover.check_now()
        state_file = Path(self.tmp) / "failover_state.json"
        self.assertTrue(state_file.exists())
        import json
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["active"], "omni")
        self.assertEqual(data["switches"][0]["from"], "nain")
        self.assertEqual(data["switches"][0]["to"], "omni")

    def test_all_down_terminal(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": False, "latency_ms": 1, "model": name, "error": "down"}
        settings.set("failover.retries", 1)
        failover.check_now()   # nain → omni
        failover.check_now()   # omni → local
        r3 = failover.check_now()  # local fails → all down
        self.assertTrue(r3["all_down"])
        self.assertEqual(failover.load_state()["all_down"], True)

    def test_no_wrap_around_reping(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": False, "latency_ms": 1, "model": name, "error": "down"}
        settings.set("failover.retries", 1)
        failover.check_now()  # → omni
        failover.check_now()  # → local
        failover.check_now()  # all_down
        # active must stay local — no re-ping of nain
        self.assertEqual(failover.load_state()["active"], "local")

    def test_manual_choice_holds(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": False, "latency_ms": 1, "model": name, "error": "down"}
        settings.set("failover.retries", 1)
        settings.set("failover.hold_minutes", 60)
        router.set_active("nain")  # manual → hold set
        r = failover.check_now()
        # with hold active, router is NOT switched even though it fails
        self.assertEqual(failover.load_state()["active"], "nain")
        self.assertIsNone(r.get("switched"))

    def test_disabled_failover_no_switch(self):
        router.ping = lambda name, timeout=8.0: {
            "ok": False, "latency_ms": 1, "model": name, "error": "down"}
        settings.set("failover.enabled", False)
        settings.set("failover.retries", 1)
        r1 = failover.check_now()
        self.assertEqual(failover.load_state()["active"], "nain")
        self.assertIsNone(r1.get("switched"))

    def test_recovery_resets_counter(self):
        state = {"down": True}
        def flaky_ping(name, timeout=8.0):
            if state["down"]:
                return {"ok": False, "latency_ms": 1, "model": name, "error": "down"}
            return {"ok": True, "latency_ms": 10, "model": name, "error": None}
        router.ping = flaky_ping
        settings.set("failover.retries", 2)
        failover.check_now()  # failure 1
        self.assertEqual(failover.load_state()["failures"], 1)
        state["down"] = False
        r = failover.check_now()  # recovers
        self.assertIsNone(r.get("switched"))
        self.assertEqual(failover.load_state()["failures"], 0)


class FailoverOrderTests(FailoverBase):
    def test_order_validation(self):
        with self.assertRaises(ValueError):
            settings.set("failover.order", ["nain", "deepmo"])
        with self.assertRaises(ValueError):
            settings.set("failover.order", "nain")  # not a list

    def test_next_in_order_exhaustion(self):
        self.assertEqual(failover._next_in_order(["nain", "omni", "local"], "nain"), "omni")
        self.assertEqual(failover._next_in_order(["nain", "omni", "local"], "omni"), "local")
        self.assertIsNone(failover._next_in_order(["nain", "omni", "local"], "local"))


if __name__ == "__main__":
    unittest.main()