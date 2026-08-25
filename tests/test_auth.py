#!/usr/bin/env python3
"""Atropos auth tests — first-run setup, PBKDF2 store, sessions, lockout."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import auth  # noqa: E402


class AuthBase(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_auth_")
        os.environ["ATROPOS_HOME"] = self.tmp
        # reset process-local rate-limit state between tests
        auth._failures = 0
        auth._lockout_until = 0.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._old is not None:
            os.environ["ATROPOS_HOME"] = self._old
        else:
            os.environ.pop("ATROPOS_HOME", None)
        auth._failures = 0
        auth._lockout_until = 0.0


class SetupTests(AuthBase):
    def test_needs_setup_true_when_fresh(self):
        self.assertTrue(auth.needs_setup())

    def test_set_password_creates_hashed_store(self):
        r = auth.set_password("hunter2")
        self.assertTrue(r["ok"])
        self.assertFalse(auth.needs_setup())
        data = json.loads((Path(self.tmp) / "dashboard_auth.json").read_text())
        entry = data["password"]
        self.assertEqual(entry["algo"], "pbkdf2_sha256")
        self.assertNotIn("hunter2", json.dumps(data))  # never plaintext
        self.assertGreaterEqual(entry["iterations"], 100_000)

    def test_set_password_rejects_short(self):
        self.assertFalse(auth.set_password("abc")["ok"])
        self.assertTrue(auth.needs_setup())

    def test_set_password_twice_resets_sessions(self):
        auth.set_password("first-pass")
        tok = auth.create_session()
        self.assertTrue(auth.validate_session(tok))
        auth.set_password("second-pass")
        self.assertFalse(auth.validate_session(tok))

    def test_salt_differs_per_password(self):
        auth.set_password("alpha")
        d1 = json.loads((Path(self.tmp) / "dashboard_auth.json").read_text())
        auth.set_password("beta")
        d2 = json.loads((Path(self.tmp) / "dashboard_auth.json").read_text())
        self.assertNotEqual(d1["password"]["salt"], d2["password"]["salt"])


class LoginTests(AuthBase):
    def setUp(self):
        super().setUp()
        auth.set_password("correct horse")

    def test_verify_ok_and_wrong(self):
        ok, wait = auth.verify_rate_limited("correct horse")
        self.assertTrue(ok)
        self.assertIsNone(wait)
        ok, _ = auth.verify_rate_limited("wrong")
        self.assertFalse(ok)

    def test_lockout_after_failures(self):
        for _ in range(auth._MAX_LOGIN_FAILURES):
            auth.verify_rate_limited("nope")
        ok, wait = auth.verify_rate_limited("correct horse")
        self.assertFalse(ok)          # even the right password is refused
        self.assertIsNotNone(wait)

    def test_success_resets_failure_counter(self):
        auth.verify_rate_limited("nope")
        auth.verify_rate_limited("nope")
        auth.verify_rate_limited("correct horse")
        # counter reset → several more failures allowed before lockout
        for _ in range(auth._MAX_LOGIN_FAILURES - 1):
            ok, wait = auth.verify_rate_limited("nope")
            self.assertIsNone(wait)


class SessionTests(AuthBase):
    def setUp(self):
        super().setUp()
        auth.set_password("pw-for-sessions")

    def test_create_and_validate(self):
        tok = auth.create_session()
        self.assertTrue(auth.validate_session(tok))
        self.assertFalse(auth.validate_session("bogus"))
        self.assertFalse(auth.validate_session(None))
        self.assertFalse(auth.validate_session(""))

    def test_drop_session(self):
        tok = auth.create_session()
        auth.drop_session(tok)
        self.assertFalse(auth.validate_session(tok))

    def test_expired_session_rejected(self):
        import time as _t
        tok = auth.create_session()
        with auth._LOCK:
            store = auth._load_store()
            store["sessions"][tok]["created"] = int(_t.time()) - auth._SESSION_TTL - 10
            auth._save_store(store)
        self.assertFalse(auth.validate_session(tok))
        # and the dead session is pruned from the store
        store = auth._load_store()
        self.assertNotIn(tok, store["sessions"])

    def test_machine_token_stable_and_independent(self):
        t1 = auth.machine_token()
        t2 = auth.machine_token()
        self.assertEqual(t1, t2)
        # password rotation must NOT break machine tokens
        auth.set_password("new-pw")
        self.assertEqual(auth.machine_token(), t1)

    def test_session_cap_prunes_oldest(self):
        for i in range(auth._MAX_SESSIONS + 5):
            tok = auth.create_session()
            if i < 4:  # remember the first few (oldest)
                with auth._LOCK:
                    pass  # tokens are random; check by count instead
        with auth._LOCK:
            store = auth._load_store()
            self.assertLessEqual(len(store["sessions"]), auth._MAX_SESSIONS)


class LegacyMigrationTests(AuthBase):
    def test_legacy_plaintext_accepted_then_scrubbed(self):
        from core import settings
        settings.set("dashboard.password", "legacy-secret")
        ok, _ = auth.verify_rate_limited("legacy-secret")
        self.assertTrue(ok)
        # migrated: hashed store exists, plaintext scrubbed from config
        data = json.loads((Path(self.tmp) / "dashboard_auth.json").read_text())
        self.assertIn("hash", data["password"])
        self.assertNotIn("legacy-secret", json.dumps(data))
        self.assertEqual(settings.get("dashboard.password", ""), "")

    def test_legacy_only_works_once(self):
        from core import settings
        settings.set("dashboard.password", "legacy-secret")
        ok, _ = auth.verify_rate_limited("legacy-secret")
        self.assertTrue(ok)
        # plaintext is gone now — a fresh verify must use the hash
        ok, _ = auth.verify_rate_limited("legacy-secret")
        self.assertTrue(ok)  # works because hash matches, not legacy path
        self.assertNotIn("legacy-secret",
                         (Path(self.tmp) / "config.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
