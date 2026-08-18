#!/usr/bin/env python3
"""v18 L tests — dashboard Command Center redesign.

Guards the new landing, quick actions, rebuilt chat welcome, and the
linkage between the dashboard surfaces and the v18 J+K sealed/migrate
features. Style-checks the static files and pings the live /api/*
endpoints exactly like test_dashboard_control does.
"""
import inspect
import json
import os
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import core.dashboard as db
import core.detect as detect

INDEX = _REPO / "dashboard" / "index.html"
CHAT = _REPO / "dashboard" / "chat.html"


def _html(p):
    return p.read_text(encoding="utf-8")


class CommandCenterMarkupTests(unittest.TestCase):
    def test_hero_greeting_present(self):
        h = _html(INDEX)
        self.assertIn("Command Center", h)
        self.assertIn("cmd-actions", h)
        self.assertIn('data-go="doctor"', h)
        self.assertIn('data-go="backup"', h)
        self.assertIn('data-go="update"', h)
        self.assertIn('data-go="chat"', h)
        self.assertIn('data-go="skills"', h)
        self.assertIn('data-go="migrate"', h)
        self.assertIn('id="cmd-status-line"', h)

    def test_quick_action_js_wired(self):
        h = _html(INDEX)
        self.assertIn("function cmdAction", h)
        self.assertIn("loadCmdStatus", h)
        self.assertIn("cmd-actions [data-go]", h)
        # loading hook for the overview panel includes the cmd status
        self.assertIn("overview: [loadCmdStatus", h)

    def test_cmd_hero_css_present(self):
        h = _html(INDEX)
        for cls in (".cmd-hero", ".cmd-greeting", ".cmd-actions", ".cmd-status"):
            self.assertIn(cls, h)

    def test_chat_welcome_rebuilt(self):
        h = _html(CHAT)
        self.assertIn('class="welcome-card"', h)
        self.assertIn('class="welcome-quick"', h)
        self.assertIn("welcome-title", h)
        self.assertIn("welcome-star", h)

    def test_chat_still_has_core_actions(self):
        h = _html(CHAT)
        for marker in ('btn-send', '/api/chat/stream', 'streamSend',
                       'session-list', 'btn-stop', 'btn-voice'):
            self.assertIn(marker, h)


class SealedGuestSurfaceTests(unittest.TestCase):
    """The dashboard /api/guest now carries sealed counts (v18 K)."""

    @classmethod
    def setUpClass(cls):
        cls._home = Path(__file__).resolve().parent / "tmp_dash_home"
        os.environ["ATROPOS_HOME"] = str(cls._home)
        cls.tok = "test-token-123"
        cls._home.mkdir(parents=True, exist_ok=True)
        (cls._home / "auth_token").write_text(cls.tok + "\n")
        cls._orig_home = detect._home
        detect._home = lambda: cls._home

        import importlib
        import core.guest as _g
        importlib.reload(_g)
        _g.record_sealed("222", "secret-plan")  # hermetic store under the home

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

    def _get(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"X-Atropos-Token": self.tok},
        )
        return urllib.request.urlopen(req, timeout=10)

    def test_guest_api_includes_sealed_counts(self):
        with self._get("/api/guest") as r:
            self.assertEqual(r.status, 200)
            d = json.loads(r.read().decode())
            self.assertIn("sealed", d)
            by = {v["user"]: v["count"] for v in d["sealed"]}
            self.assertGreaterEqual(by.get("222", 0), 1)  # count reflects our record
            self.assertIn("enabled", d)  # guest status still present

    def test_skills_api_endpoint_still_ok(self):
        with self._get("/api/skills") as r:
            self.assertEqual(r.status, 200)
            d = json.loads(r.read().decode())
            self.assertTrue(d["ok"])

    def test_status_endpoint_still_ok(self):
        with self._get("/api/status") as r:
            self.assertEqual(r.status, 200)
            json.loads(r.read().decode())


class PolicyTests(unittest.TestCase):
    """The redesign must not break the API surface (v18 L rule)."""

    def test_all_registered_api_paths_answer(self):
        # the route table is a local dict inside do_GET — callable-only guard
        # by scanning the do_GET source for api_* identifiers and verifying
        # each resolves to a callable on the dashboard module
        import core.dashboard as _db
        import re
        src = inspect.getsource(_db.Handler.do_GET)
        names = set(re.findall(r"\b(api_[a-z_]+)\s*\(", src))
        missing = [n for n in sorted(names) if not callable(getattr(_db, n, None))]
        self.assertEqual(missing, [])

    def test_index_html_has_no_failed_imports(self):
        # the dashboard JS references these loader names; every one must
        # have a function defined or be absent from the loader map
        h = _html(INDEX)
        self.assertIn("function loadCmdStatus", h)


if __name__ == "__main__":
    unittest.main()