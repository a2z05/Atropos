#!/usr/bin/env python3
"""Atropos announce feed tests — tips, dismissals, changelog, version."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import notify  # noqa: E402


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_notify_")
        os.environ["ATROPOS_HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)


class FeedTests(NotifyBase):
    def test_first_feed_has_tip_and_changelog(self):
        with mock.patch("core.notify._version", return_value="1.4.0"):
            items = notify.feed()
        types = [i["type"] for i in items]
        self.assertIn("tip", types)
        self.assertIn("changelog", types)
        tip = next(i for i in items if i["type"] == "tip")
        self.assertEqual(tip["id"], "tip:today")
        self.assertTrue(tip["dismissible"])
        self.assertEqual(tip["text"], notify._tip_of_day())
        changelog = next(i for i in items if i["type"] == "changelog")
        self.assertEqual(changelog["id"], "changelog:latest")
        self.assertIn("1.4.0", changelog["text"])

    def test_mark_changelog_seen_clears_notice(self):
        with mock.patch("core.notify._version", return_value="1.4.0"):
            notify.mark_changelog_seen("1.4.0")
            items = notify.feed()
        self.assertNotIn("changelog", [i["type"] for i in items])
        # a new version brings the notice back
        with mock.patch("core.notify._version", return_value="1.5.0"):
            items = notify.feed()
        self.assertIn("changelog", [i["type"] for i in items])

    def test_dismiss_records_and_hides(self):
        with mock.patch("core.notify._version", return_value="1.4.0"):
            items = notify.feed()
            notify.dismiss(items[0]["id"])
            again = notify.feed()
        ids = [i["id"] for i in again]
        self.assertNotIn(items[0]["id"], ids)
        data = json.loads((Path(self.tmp) / "announce.json").read_text(encoding="utf-8"))
        self.assertIn(items[0]["id"], data["dismissed"])

    def test_tip_rotates_by_day(self):
        tomorrow = date.today().toordinal() + 1
        fake_date = mock.Mock(side_effect=lambda *a, **k: date(*a, **k))
        fake_date.today.return_value = date.fromordinal(tomorrow)
        with mock.patch("core.notify._version", return_value="1.4.0"):
            items1 = notify.feed()
            tip1 = next(i["text"] for i in items1 if i["type"] == "tip")
            with mock.patch("core.notify.date", fake_date):
                items2 = notify.feed()
            tip2 = next(i["text"] for i in items2 if i["type"] == "tip")
        self.assertNotEqual(tip1, tip2)
        # persisted tip_of_day advances to the fake day
        data = json.loads((Path(self.tmp) / "announce.json").read_text(encoding="utf-8"))
        self.assertEqual(data["tip_of_day"], date.fromordinal(tomorrow).isoformat())

    def test_version_check_item_when_behind(self):
        with mock.patch("core.notify._version", return_value="1.4.0"):
            notify.mark_changelog_seen("1.4.0")  # clear changelog noise
            notify.set_version_check({"ok": True, "up_to_date": False,
                                      "behind": 3, "head": "a", "remote": "b"})
            items = notify.feed()
        types = [i["type"] for i in items]
        self.assertIn("version", types)
        ver = next(i for i in items if i["type"] == "version")
        self.assertEqual(ver["id"], "version:behind")
        self.assertIn("3", ver["text"])
        notify.dismiss("version:behind")
        with mock.patch("core.notify._version", return_value="1.4.0"):
            again = notify.feed()
        self.assertNotIn("version", [i["type"] for i in again])

    def test_no_version_item_when_up_to_date(self):
        notify.mark_changelog_seen("1.4.0")
        notify.set_version_check({"ok": True, "up_to_date": True,
                                  "behind": 0, "head": "a", "remote": "a"})
        with mock.patch("core.notify._version", return_value="1.4.0"):
            items = notify.feed()
        self.assertNotIn("version", [i["type"] for i in items])

    def test_12_bundled_tips_rotate(self):
        self.assertEqual(len(notify._TIPS), 12)
        # walk 12 consecutive ordinals: every tip appears exactly once
        base = date.today().toordinal()
        seen = set()
        for step in range(1, 13):
            fake_date = mock.Mock(side_effect=lambda *a, **k: date(*a, **k))
            fake_date.today = mock.Mock(return_value=date.fromordinal(base + step))
            with mock.patch("core.notify.date", fake_date):
                seen.add(notify._tip_of_day())
        # 12 consecutive days cover all 12 tips exactly once
        self.assertEqual(len(seen), 12)

    def test_run_version_check_persists_result(self):
        with mock.patch("core.notify.update.update_check") as uc, \
             mock.patch("core.notify.detect.hermes_agent", return_value="/repo"):
            uc.return_value = {"ok": True, "up_to_date": False, "behind": 2,
                               "head": "x", "remote": "y"}
            result = notify.run_version_check()
        self.assertTrue(result["ok"])
        data = json.loads((Path(self.tmp) / "announce.json").read_text(encoding="utf-8"))
        self.assertEqual(data["version_check"]["behind"], 2)

    def test_corrupt_store_degrades_gracefully(self):
        Path(self.tmp).mkdir(parents=True, exist_ok=True)
        (Path(self.tmp) / "announce.json").write_text("[[[", encoding="utf-8")
        with mock.patch("core.notify._version", return_value="1.4.0"):
            items = notify.feed()
        self.assertTrue(items)  # fresh defaults instead of a crash
        notify.dismiss("tip:today")  # still writable


if __name__ == "__main__":
    unittest.main()
