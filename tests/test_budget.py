#!/usr/bin/env python3
"""Atropos budget tests — usage ledger, month rollover, quota gate, failover."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import budget, config, detect, router, settings  # noqa: E402


class BudgetBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_budget_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        self._orig_set_active = router.set_active

    def tearDown(self):
        router.set_active = self._orig_set_active
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class BudgetUsageTests(BudgetBase):
    def test_record_and_usage(self):
        budget.record("nain", 100)
        budget.record("nain", 50)
        budget.record("omni", 30)
        u = budget.usage()
        self.assertEqual(u["total"], 180)
        self.assertEqual(u["per_router"]["nain"]["live"], 150)
        self.assertEqual(u["per_router"]["nain"]["total"], 150)
        self.assertEqual(u["per_router"]["omni"]["total"], 30)

    def test_ledger_persisted(self):
        budget.record("nain", 7)
        data = json.loads(budget.usage_path().read_text(encoding="utf-8"))
        self.assertIn(budget.month_key(), data["nain"])
        self.assertEqual(data["nain"][budget.month_key()], 7)

    def test_month_rollover(self):
        budget.record("nain", 10)
        last_month = datetime(2020, 1, 15)
        budget.record("nain", 5)
        key = budget.month_key(last_month)
        data = json.loads(budget.usage_path().read_text(encoding="utf-8"))
        data["nain"][key] = 200  # simulate last month's usage
        budget.usage_path().write_text(json.dumps(data), encoding="utf-8")
        u = budget.usage(key)
        self.assertEqual(u["total"], 200)  # last month only
        cur = budget.usage()
        self.assertEqual(cur["per_router"]["nain"]["total"], 15)  # this month only

    def test_missing_state_db_counts_zero(self):
        u = budget.usage()
        self.assertEqual(u["per_router"]["local"]["estimated"], 0)
        self.assertEqual(u["per_router"]["local"]["total"], 0)

    def test_state_db_estimate(self):
        # hermes_home()/data/state.db
        dbdir = Path(self.tmp) / ".hermes" / "data"
        dbdir.mkdir(parents=True, exist_ok=True)
        db = dbdir / "state.db"
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (id TEXT, router TEXT, prompt_tokens INT, completion_tokens INT)")
        conn.execute("INSERT INTO sessions VALUES ('a', 'nain', 100, 20)")
        conn.execute("INSERT INTO sessions VALUES ('b', 'omni', 10, 5)")
        conn.commit()
        conn.close()
        u = budget.usage()
        self.assertEqual(u["per_router"]["nain"]["estimated"], 120)
        self.assertEqual(u["per_router"]["omni"]["estimated"], 15)
        # corrupt db -> 0, never raises
        db.write_bytes(b"\x00\x01not-a-db")
        self.assertEqual(budget.estimate_from_state_db("nain"), 0)


class BudgetGateTests(BudgetBase):
    def _enable(self, budget_tokens, auto_failover=False):
        settings.set("budget.enabled", True)
        settings.set("budget.monthly_tokens", budget_tokens)
        settings.set("budget.auto_failover", auto_failover)

    def test_over_budget(self):
        budget.record("nain", 100)
        self.assertFalse(budget.over_budget())  # disabled by default
        self._enable(150)
        self.assertFalse(budget.over_budget())
        budget.record("nain", 50)
        self.assertTrue(budget.over_budget())
        u = budget.usage()
        self.assertTrue(u["over"])
        self.assertEqual(u["pct"], 100.0)

    def test_budget_zero_means_unlimited(self):
        self._enable(0)
        budget.record("nain", 10**9)
        self.assertFalse(budget.over_budget())

    def test_check_and_alert_over_budget(self):
        self._enable(100)
        budget.record("nain", 100)
        with mock.patch("core.alerts.send_alert", return_value=True) as sa:
            res = budget.check_and_alert()
        self.assertTrue(res["alerted"])
        sa.assert_called_once()
        self.assertIn("100", sa.call_args[0][0])
        self.assertFalse(res["failed_over"])  # auto_failover off

    def test_check_and_alert_under_budget(self):
        self._enable(1000)
        budget.record("nain", 10)
        with mock.patch("core.alerts.send_alert", return_value=True) as sa:
            res = budget.check_and_alert()
        self.assertFalse(res["alerted"])
        sa.assert_not_called()

    def test_alerts_disabled_skips_send(self):
        self._enable(100)
        settings.set("alerts.enabled", False)
        budget.record("nain", 100)
        with mock.patch("core.alerts.send_alert", return_value=True) as sa:
            res = budget.check_and_alert()
        self.assertFalse(res["alerted"])
        sa.assert_not_called()

    def test_auto_failover_switches_to_cheapest(self):
        self._enable(100, auto_failover=True)
        budget.record("nain", 100)
        budget.record("omni", 5)
        calls = []
        router.set_active = lambda name: calls.append(name) or router.ROUTERS[name]
        with mock.patch("core.alerts.send_alert", return_value=True):
            res = budget.check_and_alert()
        self.assertTrue(res["alerted"])
        self.assertTrue(res["failed_over"])
        self.assertEqual(calls, ["local"])  # cheapest router wins (0 usage)
        self.assertEqual(res["active_router"], "local")

    def test_auto_failover_noop_when_active_cheapest(self):
        self._enable(100, auto_failover=True)
        budget.record("omni", 100)
        budget.record("nain", 200)
        budget.record("local", 5)
        calls = []
        router.set_active = lambda name: calls.append(name) or router.ROUTERS[name]
        with mock.patch("core.alerts.send_alert", return_value=True):
            res = budget.check_and_alert()
        self.assertTrue(res["failed_over"])
        self.assertEqual(calls, ["local"])  # local is cheapest

    def test_send_alert_error_is_contained(self):
        self._enable(100, auto_failover=True)
        budget.record("nain", 100)
        with mock.patch("core.alerts.send_alert", side_effect=Exception("telegram down")):
            res = budget.check_and_alert()
        self.assertFalse(res["alerted"])
        self.assertTrue(res["failed_over"])


if __name__ == "__main__":
    unittest.main()
