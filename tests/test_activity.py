#!/usr/bin/env python3
"""Atropos activity tests — timeline logging, rotation, today/feed grouping."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import activity, detect  # noqa: E402


class ActivityBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_activity_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        self.path = detect.atropos_home() / activity.ACTIVITY_FILE

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class ActivityLogTests(ActivityBase):
    def _write_lines(self, count, payload="x"):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            for _ in range(count):
                f.write(json.dumps({"ts": "2026-08-15T00:00:00Z", "event": "rot",
                                    "detail": payload}) + "\n")

    def test_log_appends_line(self):
        entry = activity.log("update", "applied v1.1.0")
        self.assertEqual(entry["event"], "update")
        self.assertEqual(entry["detail"], "applied v1.1.0")
        self.assertIn("ts", entry)
        with open(self.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["event"], "update")

    def test_append_only(self):
        activity.log("a")
        activity.log("b", "detail-b")
        with open(self.path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["event"], "b")

    def test_rotation_truncates_to_last_1mb(self):
        # seed >1 MB of history (a single write), then a few appends
        # exceed the 1 MB cap and trigger rotation (261 bytes/line)
        big = "x" * 200
        self._write_lines(5000, big)  # ~1.31 MB
        self.assertGreaterEqual(self.path.stat().st_size, 1024 * 1024)
        with mock.patch.object(activity, "_max_mb", return_value=1):
            activity.log("rot", big)
            activity.log("rot", big)
        size_after = self.path.stat().st_size
        # truncation keeps the last 1 MB (rounded up to a line boundary)
        self.assertLessEqual(size_after, 1024 * 1024 + 2 * 261)
        self.assertLess(size_after, 1310000)  # and far below the seeded size
        # still valid JSONL (whole lines only)
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                json.loads(line)

    def test_rotation_respects_configured_max_mb(self):
        big = "y" * 200
        self._write_lines(5000, big)  # ~1.31 MB seeded
        with mock.patch.object(activity, "_max_mb", return_value=1):
            activity.log("rot", big)
        self.assertLessEqual(self.path.stat().st_size, 1024 * 1024 + 2 * 261)
        self.assertLess(self.path.stat().st_size, 1310000)

    def test_no_rotation_below_cap(self):
        self._write_lines(10, "z")
        with mock.patch.object(activity, "_max_mb", return_value=5):
            activity.log("rot", "z")
        self.assertGreater(self.path.stat().st_size, 100)  # appends accumulate


class ActivityTodayTests(ActivityBase):
    def test_today_returns_last_24h(self):
        path = detect.atropos_home() / activity.ACTIVITY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        with open(path, "w", encoding="utf-8") as f:
            for i, ev in enumerate(("update", "alert", "backup")):
                ts = (now - timedelta(seconds=60 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(json.dumps({"ts": ts, "event": ev,
                                    "detail": "disk 85%" if ev == "alert" else ""}) + "\n")
        rows = activity.today()
        self.assertEqual(len(rows), 3)
        self.assertEqual([e["event"] for e in rows], ["backup", "alert", "update"])  # newest first
        self.assertEqual(rows[1]["detail"], "disk 85%")

    def test_today_filters_stale_entries(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = detect.atropos_home() / activity.ACTIVITY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": old_ts, "event": "old", "detail": ""}) + "\n",
                        encoding="utf-8")
        activity.log("fresh")
        rows = activity.today()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "fresh")

    def test_today_bounded_to_500(self):
        path = detect.atropos_home() / activity.ACTIVITY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        with open(path, "w", encoding="utf-8") as f:
            for i in range(600):
                ts = (now - timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(json.dumps({"ts": ts, "event": "burst", "detail": str(i)}) + "\n")
        rows = activity.today()
        self.assertEqual(len(rows), 500)

    def test_today_ignores_corrupt_lines(self):
        path = detect.atropos_home() / activity.ACTIVITY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n{\"ts\": \"2026-01-01T00:00:00Z\"}\n", encoding="utf-8")
        activity.log("ok")
        self.assertEqual([e["event"] for e in activity.today()], ["ok"])


class ActivityFeedTests(ActivityBase):
    def test_feed_groups_by_category(self):
        activity.log("update", "v1.2.0")
        activity.log("apply", "hack")
        activity.log("alert", "disk")
        activity.log("backup", "daily")
        activity.log("jailbreak", "bypass")
        activity.log("session", "start")
        activity.log("router", "failover")
        activity.log("fleet", "box down")
        activity.log("custom-thing")
        feed = activity.feed()
        self.assertEqual(feed["updates"], 2)
        self.assertEqual(feed["alerts"], 1)
        self.assertEqual(feed["backups"], 1)
        self.assertEqual(feed["jailbreaks"], 1)
        self.assertEqual(feed["sessions"], 1)
        self.assertEqual(feed["routers"], 2)
        events = feed["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "custom-thing")

    def test_feed_empty(self):
        feed = activity.feed()
        self.assertEqual(feed["updates"], 0)
        self.assertEqual(feed["events"], [])


if __name__ == "__main__":
    unittest.main()
