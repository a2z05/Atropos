#!/usr/bin/env python3
"""Atropos links tests — create/verify one-use/expiry/revoke, token hashing."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import detect, links  # noqa: E402


class LinksBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_links_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def _raw_registry(self):
        return json.loads(links.links_path().read_text(encoding="utf-8"))


class LinksCreateTests(LinksBase):
    def test_create_shape(self):
        before = time.time()
        link = links.create("session-42")
        after = time.time()
        self.assertTrue(link["token"])
        self.assertEqual(link["url"], f"/chat?share={link['token']}")
        self.assertGreaterEqual(link["expires"], before + 3600 - 1)
        self.assertLessEqual(link["expires"], after + 3600 + 1)
        self.assertEqual(len(link["token"]), 16)  # token_urlsafe(12) → 16 chars

    def test_create_requires_session(self):
        with self.assertRaises(ValueError):
            links.create("")
        with self.assertRaises(ValueError):
            links.create("   ")

    def test_custom_ttl(self):
        link = links.create("s1", ttl_hours=2)
        self.assertAlmostEqual(link["expires"] - time.time(), 7200, delta=10)

    def test_token_stored_hashed_only(self):
        link = links.create("s1")
        registry = self._raw_registry()
        self.assertEqual(len(registry), 1)
        stored = next(iter(registry.values()))
        self.assertEqual(stored["session_id"], "s1")
        self.assertEqual(stored["kind"], "chat")
        self.assertFalse(stored["used"])
        raw_tokens = [v for v in registry if v == link["token"]]
        self.assertEqual(raw_tokens, [])  # plaintext never stored
        self.assertEqual(next(iter(registry)), links._hash(link["token"]))


class LinksVerifyTests(LinksBase):
    def test_verify_ok(self):
        link = links.create("s1")
        res = links.verify(link["token"])
        self.assertEqual(res, {"ok": True, "session_id": "s1"})

    def test_one_use_enforced(self):
        link = links.create("s1")
        self.assertTrue(links.verify(link["token"])["ok"])
        res2 = links.verify(link["token"])
        self.assertFalse(res2["ok"])
        self.assertIn("used", res2["error"])
        # a fresh link still works
        link2 = links.create("s2")
        self.assertTrue(links.verify(link2["token"])["ok"])

    def test_expired_link_rejected(self):
        link = links.create("s1", ttl_hours=2)
        registry = self._raw_registry()
        key = links._hash(link["token"])
        registry[key]["expires"] = time.time() - 1
        links.links_path().write_text(json.dumps(registry), encoding="utf-8")
        res = links.verify(link["token"])
        self.assertFalse(res["ok"])
        self.assertIn("expired", res["error"])

    def test_unknown_token(self):
        res = links.verify("bogus-token")
        self.assertFalse(res["ok"])
        self.assertIn("invalid", res["error"])

    def test_empty_token(self):
        self.assertFalse(links.verify("")["ok"])


class LinksRevokeListTests(LinksBase):
    def test_revoke(self):
        link = links.create("s1")
        self.assertTrue(links.revoke(link["token"])["ok"])
        self.assertEqual(links.revoke(link["token"]), {"ok": False, "error": "link not found"})
        self.assertFalse(links.verify(link["token"])["ok"])
        self.assertEqual(self._raw_registry(), {})

    def test_list_links(self):
        l1 = links.create("s1", ttl_hours=1)
        l2 = links.create("s2", ttl_hours=1)
        rows = links.list_links()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(len(r["id"]), 12)  # hash prefix
        self.assertFalse(any(r["used"] for r in rows))
        # consume one
        links.verify(l1["token"])
        active = links.list_links(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["session_id"], "s2")
        links.revoke(l2["token"])
        self.assertEqual(links.list_links(active_only=True), [])


if __name__ == "__main__":
    unittest.main()
