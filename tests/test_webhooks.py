#!/usr/bin/env python3
"""Atropos webhooks tests — registry CRUD, delivery, error isolation."""

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

from core import webhooks  # noqa: E402


class WebhookBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_webhooks_")
        self.home = Path(self.tmp)
        os.environ["ATROPOS_HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.home / ".hermes")
        self._urlopen = webhooks.urllib.request.urlopen
        # keep any real network off the developer's machine
        webhooks.urllib.request.urlopen = mock.MagicMock(
            side_effect=OSError("network disabled in tests"))

    def tearDown(self):
        webhooks.urllib.request.urlopen = self._urlopen
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def registry(self) -> list:
        p = self.home / "webhooks.json"
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def fake_urlopen(self, status=200):
        """Patch urlopen with a fake 200 response; returns the patcher."""
        fake = mock.MagicMock()
        fake.status = status
        fake.__enter__.return_value = fake
        webhooks.urllib.request.urlopen = mock.MagicMock(return_value=fake)
        return webhooks.urllib.request.urlopen

    def captured_request(self, fake_urlopen_call) -> dict:
        """Decode the JSON body of the request passed to fake urlopen."""
        req = fake_urlopen_call.call_args[0][0]
        return json.loads(req.data.decode("utf-8"))


class RegistryTests(WebhookBase):
    def test_add_list_enable_disable(self):
        webhooks.add("alerts", "https://example.com/hook", ["alerts", "backup"])
        hooks = self.registry()
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["name"], "alerts")
        self.assertEqual(hooks[0]["url"], "https://example.com/hook")
        self.assertEqual(hooks[0]["events"], ["alerts", "backup"])
        self.assertTrue(hooks[0]["enabled"])
        self.assertIsNone(hooks[0]["last_sent"])
        self.assertIsNone(hooks[0]["last_status"])

        webhooks.disable("alerts")
        self.assertFalse(self.registry()[0]["enabled"])
        webhooks.enable("alerts")
        self.assertTrue(self.registry()[0]["enabled"])

        names = [h["name"] for h in webhooks.list_webhooks()]
        self.assertEqual(names, ["alerts"])

    def test_add_validation(self):
        # bad names
        for bad in ("../evil", "a/b", "", "x" * 65):
            with self.assertRaises(ValueError):
                webhooks.add(bad, "https://example.com/hook")
        # bad urls
        for url in ("ftp://example.com", "file:///etc/passwd", "not-a-url",
                    "https://", ""):
            with self.assertRaises(ValueError):
                webhooks.add("ok-name", url)
        self.assertEqual(self.registry(), [])

    def test_add_duplicate_raises(self):
        webhooks.add("alerts", "https://example.com/hook")
        with self.assertRaises(ValueError):
            webhooks.add("alerts", "https://other.example/hook")

    def test_remove(self):
        webhooks.add("alerts", "https://example.com/hook")
        res = webhooks.remove("alerts")
        self.assertTrue(res["ok"])
        self.assertEqual(self.registry(), [])
        with self.assertRaises(FileNotFoundError):
            webhooks.remove("alerts")

    def test_stats(self):
        webhooks.add("a", "https://example.com/a", ["x"])
        webhooks.add("b", "https://example.com/b", ["x"])
        webhooks.disable("b")
        s = webhooks.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["enabled"], 1)


class DeliveryTests(WebhookBase):
    def test_trigger_posts_matching_hooks_only(self):
        webhooks.add("alerts", "https://example.com/alerts", ["alerts"])
        webhooks.add("backup", "https://example.com/backup", ["backup"])
        fake = self.fake_urlopen(200)

        res = webhooks.trigger("alerts", {"level": "warn"})

        self.assertEqual(res["event"], "alerts")
        self.assertEqual(res["delivered"], ["alerts"])
        self.assertEqual(res["skipped"], ["backup"])
        self.assertEqual(res["failed"], [])
        self.assertEqual(fake.call_count, 1)
        body = self.captured_request(fake)
        self.assertEqual(body["event"], "alerts")
        self.assertEqual(body["level"], "warn")

    def test_trigger_all_events_hook(self):
        # a hook with no events matches nothing — explicit subscription only
        webhooks.add("silent", "https://example.com/silent", [])
        fake = self.fake_urlopen(200)
        res = webhooks.trigger("alerts")
        self.assertEqual(res["delivered"], [])
        self.assertEqual(res["skipped"], ["silent"])
        fake.assert_not_called()

    def test_trigger_records_last_sent_and_status(self):
        webhooks.add("alerts", "https://example.com/alerts", ["alerts"])
        self.fake_urlopen(200)
        webhooks.trigger("alerts")
        hook = self.registry()[0]
        self.assertIsNotNone(hook["last_sent"])
        self.assertEqual(hook["last_status"], 200)

    def test_trigger_disabled_hook_skipped(self):
        webhooks.add("alerts", "https://example.com/alerts", ["alerts"])
        webhooks.disable("alerts")
        fake = self.fake_urlopen(200)
        res = webhooks.trigger("alerts")
        self.assertEqual(res["delivered"], [])
        self.assertEqual(res["skipped"], ["alerts"])
        fake.assert_not_called()

    def test_error_isolation(self):
        # first hook is permanently bad (every attempt fails), second
        # succeeds — nothing propagates, failures don't block others,
        # and the bad hook is retried (retry-with-backoff adoption)
        webhooks.add("bad", "https://example.com/bad", ["alerts"])
        webhooks.add("good", "https://example.com/good", ["alerts"])

        calls = {"n": 0}
        def bad_urlopen(req, timeout=None):
            calls["n"] += 1
            if req.full_url and "bad" in req.full_url:
                raise OSError("connection refused")
            fake = mock.MagicMock()
            fake.status = 200
            fake.__enter__.return_value = fake
            return fake
        with mock.patch.object(webhooks.urllib.request, "urlopen",
                               side_effect=bad_urlopen) as patched:
            res = webhooks.trigger("alerts")
        # bad: 1 initial + retries; good: 1. Isolation intact either way.
        self.assertEqual(res["delivered"], ["good"])
        self.assertEqual(res["failed"], ["bad"])
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(res["errors"][0]["name"], "bad")
        self.assertGreaterEqual(patched.call_count, 2)
        # per-hook records are still written for the failure
        by_name = {h["name"]: h for h in self.registry()}
        self.assertEqual(by_name["bad"]["last_status"], None)
        self.assertIsNotNone(by_name["good"]["last_status"])

    def test_http_error_isolated(self):
        import urllib.error
        webhooks.add("err", "https://example.com/err", ["alerts"])
        webhooks.urllib.request.urlopen = mock.MagicMock(
            side_effect=urllib.error.HTTPError(
                "https://example.com/err", 502, "bad gateway", None, io.BytesIO(b"")))
        res = webhooks.trigger("alerts")  # must not raise
        self.assertEqual(res["failed"], ["err"])
        hook = self.registry()[0]
        self.assertEqual(hook["last_status"], 502)

    def test_ping_sends_ping_payload(self):
        webhooks.add("alerts", "https://example.com/alerts", ["alerts"])
        fake = self.fake_urlopen(200)
        res = webhooks.ping("alerts")
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], 200)
        self.assertIsNone(res["error"])
        body = self.captured_request(fake)
        self.assertEqual(body["event"], "ping")
        hook = self.registry()[0]
        self.assertEqual(hook["last_status"], 200)

    def test_ping_unknown_raises(self):
        with self.assertRaises(FileNotFoundError):
            webhooks.ping("ghost")


if __name__ == "__main__":
    unittest.main()
