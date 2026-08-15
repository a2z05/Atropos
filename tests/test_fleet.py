#!/usr/bin/env python3
"""Atropos fleet tests — registry add/remove, ping isolation, status rows."""

import io
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

from core import fleet, detect  # noqa: E402


class FleetBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_fleet_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        self.reg = detect.atropos_home() / "fleet.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def _ok_resp(self):
        resp = mock.Mock()
        resp.status = 200
        resp.read.return_value = json.dumps({"version": "1.1.0", "router": "nain"}).encode()
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        return resp


class FleetAddRemoveTests(FleetBase):
    def test_add_validates_url(self):
        with self.assertRaises(ValueError):
            fleet.add("box", "not-a-url")
        with self.assertRaises(ValueError):
            fleet.add("box", "ftp://host/x")
        with self.assertRaises(ValueError):
            fleet.add("", "http://host/x")
        box = fleet.add("office", "https://box.local:8787/", "secret")
        self.assertEqual(box["url"], "https://box.local:8787")  # trailing / stripped
        self.assertTrue(box["id"])

    def test_add_duplicate_url_rejected(self):
        fleet.add("one", "http://host:1")
        with self.assertRaises(ValueError):
            fleet.add("two", "http://host:1/")

    def test_remove_by_id(self):
        box = fleet.add("office", "http://host:1")
        res = fleet.remove(box["id"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], "office")
        self.assertEqual(fleet.remove(box["id"]), {"ok": False, "error": f"box not found: {box['id']}"})

    def test_registry_persisted(self):
        fleet.add("office", "http://host:1", "tok")
        data = json.loads(self.reg.read_text(encoding="utf-8"))
        self.assertEqual(len(data["boxes"]), 1)
        self.assertEqual(data["boxes"][0]["name"], "office")
        self.assertEqual(data["boxes"][0]["token"], "tok")
        fleet.add("lab", "http://host:2")
        data = json.loads(self.reg.read_text(encoding="utf-8"))
        self.assertEqual(len(data["boxes"]), 2)


class FleetPingTests(FleetBase):
    def test_ping_success(self):
        box = fleet.add("office", "http://host:1", "tok")
        with mock.patch("urllib.request.urlopen", return_value=self._ok_resp()) as m:
            results = fleet.ping(box["id"])
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["version"], "1.1.0")
        self.assertEqual(r["router"], "nain")
        self.assertIsInstance(r["latency_ms"], int)
        req = m.call_args[0][0]
        self.assertEqual(req.get_header("X-Atropos-Token"), "tok")
        self.assertEqual(req.get_full_url(), "http://host:1/api/status")

    def test_ping_all(self):
        fleet.add("a", "http://host:1")
        fleet.add("b", "http://host:2")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            results = fleet.ping()
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertFalse(r["ok"])
            self.assertIn("refused", r["error"])
        self.assertEqual(fleet.status_all()[0]["last_status"], "down")

    def test_ping_failure_isolation(self):
        box = fleet.add("a", "http://host:1")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            r = fleet.ping(box["id"])[0]
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])
        self.assertIsNone(r["latency_ms"])
        self.assertFalse(fleet.ping("missing-id")[0]["ok"])

    def test_ping_updates_last_seen(self):
        box = fleet.add("a", "http://host:1")
        with mock.patch("urllib.request.urlopen", return_value=self._ok_resp()):
            fleet.ping(box["id"])
        rows = fleet.status_all()
        self.assertEqual(rows[0]["last_status"], "ok")
        self.assertIsNotNone(rows[0]["last_seen"])

    def test_ping_http_error(self):
        box = fleet.add("a", "http://host:1")
        from urllib.error import HTTPError
        with mock.patch("urllib.request.urlopen", side_effect=HTTPError(
                "http://host:1/api/status", 401, "Unauthorized", None, None)):
            r = fleet.ping(box["id"])[0]
        self.assertFalse(r["ok"])
        self.assertIn("401", r["error"])

    def test_ping_never_raises(self):
        fleet.add("a", "http://host:1")
        with mock.patch("urllib.request.urlopen", side_effect=Exception("network gone")):
            for r in fleet.ping():
                self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
