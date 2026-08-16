#!/usr/bin/env python3
"""BETA 1.4.1 control-room checks — build field, badge wiring, version strings."""
import json
import os
import re
import sys
import threading
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import core.dashboard as db
import core.detect as detect


class VersionBuildTests(unittest.TestCase):
    def test_settings_carries_build(self):
        d = db.api_settings()
        self.assertTrue(d["ok"])
        self.assertEqual(d["build"], _shim_version())

    def test_version_carries_build(self):
        d = db.api_version()
        self.assertEqual(d["build"], _shim_version())
        self.assertEqual(d["version"], _shim_version())


class BadgeMarkupTests(unittest.TestCase):
    def test_beta_badge_in_dashboard(self):
        html = (Path(__file__).parent.parent / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="beta-badge"', html)
        self.assertIn("BETA", html)
        # wiring: version loader hides badge on non-beta builds, settings loader respects toggle
        self.assertIn("includes('-beta')", html)
        self.assertIn("d.beta_badge", html)

    def test_beta_badge_in_chat(self):
        html = (Path(__file__).parent.parent / "dashboard" / "chat.html").read_text(encoding="utf-8")
        self.assertIn('id="chat-beta"', html)

    def test_version_file_is_beta(self):
        v = (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        self.assertIn("-beta", v)

    def test_mobile_complete_dashboard(self):
        html = (Path(__file__).parent.parent / "dashboard" / "index.html").read_text(encoding="utf-8")
        # bottom nav with five tabs, phone breakpoints, bottom sheets, safe-area
        self.assertIn('id="bottomnav"', html)
        for tab in ("overview", "chat", "sessions", "settings", "more"):
            self.assertIn(f'data-nav="{tab}"', html)
        self.assertIn("@media (max-width: 430px)", html)
        self.assertIn("safe-area-inset-bottom", html)
        self.assertIn("viewport-fit=cover", html)
        self.assertIn("sheetUp", html)
        self.assertIn(".trace-overlay.open { align-items: flex-end; }", html)
        self.assertIn("min-height: 44px", html)

    def test_package_json_matches_version(self):
        pkg = json.loads((Path(__file__).parent.parent / "package.json").read_text(encoding="utf-8"))
        v = (Path(__file__).parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(pkg["version"], v)


class PanelEndpointTests(unittest.TestCase):
    """Every registered panel endpoint answers 200 JSON."""

    @classmethod
    def setUpClass(cls):
        from core import detect
        cls._home = Path(__file__).resolve().parent / "tmp_dash_home"
        os.environ["ATROPOS_HOME"] = str(cls._home)
        cls.tok = "test-token-123"
        cls._home.mkdir(parents=True, exist_ok=True)
        (cls._home / "auth_token").write_text(cls.tok + "\n")
        cls._orig_home = detect._home
        detect._home = lambda: cls._home  # auth reads detect.atropos_home()

        class TestHandler(db.Handler):
            def _auth(self):
                return self.headers.get("X-Atropos-Token") == cls.tok

        cls.httpd = db.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.t.join(timeout=5)
        del os.environ["ATROPOS_HOME"]
        detect._home = cls._orig_home

    @staticmethod
    def _get(path):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{PanelEndpointTests.port}{path}",
            headers={"X-Atropos-Token": PanelEndpointTests.tok},
        )
        return urllib.request.urlopen(req, timeout=10)

    def test_version_endpoint(self):
        with self._get("/api/version") as r:
            self.assertEqual(r.status, 200)
            d = json.loads(r.read().decode())
            self.assertIn("build", d)
            self.assertIn("-beta", d["build"])

    def test_settings_endpoint_has_build_badge(self):
        with self._get("/api/settings") as r:
            self.assertEqual(r.status, 200)
            d = json.loads(r.read().decode())
            self.assertEqual(d["build"], _shim_version())
            self.assertIn("beta_badge", d)

    def test_auth_without_token_rejected(self):
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/version", timeout=10)
            self.fail("should 401")
        except urllib.error.HTTPError as e:
            e.close()
            self.assertEqual(e.code, 401)


def _shim_version():
    return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()


if __name__ == "__main__":
    unittest.main()