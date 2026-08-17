#!/usr/bin/env python3
"""Railway integration (v18 B) — status, volume, deploy lifecycle, health."""
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

from core import railway, detect  # noqa: E402


class RailwayBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_railway_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["RAILWAY_PROJECT_ID"] = "prj-test"
        os.environ["RAILWAY_SERVICE_ID"] = "srv-test"
        os.environ["RAILWAY_ENVIRONMENT_ID"] = "env-test"
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "atropos.up.railway.app"
        os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = self.tmp
        os.environ["RAILWAY_REPLICA_ID"] = "0"
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "abc123"
        os.environ["RAILWAY_GIT_BRANCH"] = "main"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("ATROPOS_HOME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
                  "RAILWAY_ENVIRONMENT_ID", "RAILWAY_PUBLIC_DOMAIN",
                  "RAILWAY_VOLUME_MOUNT_PATH", "RAILWAY_REPLICA_ID",
                  "RAILWAY_GIT_COMMIT_SHA", "RAILWAY_GIT_BRANCH"):
            if self._a is not None and k == "ATROPOS_HOME":
                os.environ[k] = self._a
            else:
                os.environ.pop(k, None)


class StatusTests(RailwayBase):
    def test_status_shape(self):
        s = railway.status()
        self.assertTrue(s["ok"])
        self.assertEqual(s["project"], "prj-test")
        self.assertEqual(s["domain"], "atropos.up.railway.app")
        self.assertEqual(s["replica"], "0")
        self.assertEqual(s["git_commit"], "abc123")

    def test_is_railway(self):
        self.assertTrue(railway.is_railway())


class VolumeTests(RailwayBase):
    def test_volume_usage(self):
        v = railway.volume_usage()
        self.assertTrue(v["ok"])
        self.assertTrue(0 <= v["used_pct"] <= 100)
        self.assertIn("used", v)
        self.assertIn("total", v)

    def test_volume_simulated_warn(self):
        # 999-exabyte fake filesystem → used_pct > 80 → warn True
        import collections
        du = collections.namedtuple("usage", "total used free")
        with mock.patch("shutil.disk_usage",
                        return_value=du(9 * 10**9, 8.5 * 10**9, 10**9)):
            v = railway.volume_usage()
        self.assertTrue(v["warn"])


class DeployTests(RailwayBase):
    def test_first_deploy_snapshots_and_marks(self):
        r = railway.check_deploy()
        self.assertTrue(r["ok"])
        self.assertTrue(r["changed"])
        self.assertIn("snapshot_id", r)
        self.assertEqual(r["sha"], "abc123")
        # second call: unchanged
        r2 = railway.check_deploy()
        self.assertEqual(r2["skipped"], "no commit change")

    def test_no_commit_change_skips(self):
        railway.check_deploy()
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "def456"
        r = railway.check_deploy()
        self.assertTrue(r["changed"])
        self.assertEqual(r["sha"], "def456")

    def test_not_on_railway_skips(self):
        del os.environ["RAILWAY_PROJECT_ID"]
        del os.environ["RAILWAY_ENVIRONMENT_ID"]
        r = railway.check_deploy()
        self.assertEqual(r["skipped"], "not on railway")

    def test_last_deploy_present(self):
        railway.check_deploy()
        d = railway.last_deploy()
        self.assertTrue(d["present"])
        self.assertEqual(d["sha"], "abc123")


class StaticTests(RailwayBase):
    def test_doctor_extra_shape(self):
        checks = railway.doctor_extra()
        self.assertEqual(len(checks), 2)
        names = {c["name"] for c in checks}
        self.assertIn("railway volume", names)
        self.assertIn("stale pids", names)

    def test_stale_pid_count(self):
        (detect.atropos_home() / "dead.pid").write_text("999999999\n")
        self.assertGreaterEqual(railway._stale_pid_count(), 1)

    def test_api_health_endpoint(self):
        from core import dashboard
        d = dashboard.api_health()
        self.assertEqual(d["status"], "ok")
        self.assertIn("version", d)
        self.assertEqual(d["cloud"], "railway")
        self.assertTrue(d["railway"])


if __name__ == "__main__":
    unittest.main()