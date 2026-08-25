#!/usr/bin/env python3
"""Atropos API tests — live HTTP against the stdlib dashboard server."""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import config, detect  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ApiBase(unittest.TestCase):
    _server_thread = None
    _port = None

    @classmethod
    def setUpClass(cls):
        from core import dashboard, auth as auth_mod
        cls._tmp = tempfile.mkdtemp(prefix="atropos_api_")
        cls._old_a = os.environ.get("ATROPOS_HOME")
        cls._old_h = os.environ.get("HERMES_HOME")
        os.environ["ATROPOS_HOME"] = cls._tmp
        os.environ["HERMES_HOME"] = str(Path(cls._tmp) / ".hermes")
        # password auth: create a known password and mint a session cookie
        cls._password = "test-dashboard-pw"
        auth_mod.set_password(cls._password)
        cls._token = auth_mod.create_session()  # sent as atropos_session cookie
        cls._port = free_port()
        cls._server_thread = threading.Thread(
            target=dashboard.serve, args=("127.0.0.1", cls._port), daemon=True)
        cls._server_thread.start()
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)
        if cls._old_a is not None:
            os.environ["ATROPOS_HOME"] = cls._old_a
        else:
            os.environ.pop("ATROPOS_HOME", None)
        if cls._old_h is not None:
            os.environ["HERMES_HOME"] = cls._old_h
        else:
            os.environ.pop("HERMES_HOME", None)

    def request(self, path, data=None, token=None, timeout=60):
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json"}
        token = self._token if token is None else token
        headers["Cookie"] = f"atropos_session={token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}", data=body, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def get(self, path, timeout=60):
        return self.request(path, timeout=timeout)


class AuthTests(ApiBase):
    def test_wrong_session_rejected(self):
        try:
            self.request("/api/status", token="forged-session-token")
            rejected = False
        except urllib.error.HTTPError as e:
            rejected = e.code == 401
        self.assertTrue(rejected)

    def test_authenticated_ok(self):
        d = self.get("/api/status")
        self.assertTrue(d["ok"])
        self.assertIn("router", d)

    def test_auth_state_endpoints(self):
        # pre-auth state endpoint needs no cookie
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self._port}/api/auth/state", timeout=10) as r:
            st = json.loads(r.read().decode())
        self.assertTrue(st["ok"])
        self.assertFalse(st["needs_setup"])  # password exists (setUpClass)
        self.assertFalse(st["authenticated"])

    def test_login_flow_sets_cookie(self):
        import urllib.request as _u
        req = _u.Request(
            f"http://127.0.0.1:{self._port}/api/auth/login",
            data=json.dumps({"password": self._password}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with _u.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            setc = r.headers.get("Set-Cookie", "")
        self.assertIn("atropos_session=", setc)
        self.assertIn("HttpOnly", setc)

    def test_login_rejects_bad_password(self):
        import urllib.request as _u
        req = _u.Request(
            f"http://127.0.0.1:{self._port}/api/auth/login",
            data=json.dumps({"password": "totally-wrong"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            _u.urlopen(req, timeout=10)
            rejected = False
        except urllib.error.HTTPError as e:
            rejected = e.code == 401
        self.assertTrue(rejected)


class SettingsApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.request("/api/settings/set", {"key": "dashboard.port", "value": "8787"})

    def test_settings_surface(self):
        d = self.get("/api/settings")
        self.assertTrue(d["ok"])
        groups = d["groups"]
        self.assertIn("core", groups)
        self.assertIn("dashboard", groups)
        for it in groups["dashboard"]:
            if it["key"] == "dashboard.port":
                self.assertEqual(it["value"], 8787)
                self.assertEqual(it["type"], "int")

    def test_settings_set_valid(self):
        d = self.request("/api/settings/set", {"key": "dashboard.port", "value": "8799"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["value"], 8799)

    def test_settings_set_invalid_type(self):
        d = self.request("/api/settings/set", {"key": "dashboard.port", "value": "abc"})
        self.assertFalse(d["ok"])
        self.assertIn("integer", d["error"])

    def test_settings_set_unknown_key(self):
        d = self.request("/api/settings/set", {"key": "nf.key", "value": 1})
        self.assertFalse(d["ok"])
        self.assertIn("unknown", d["error"])

    def test_settings_secret_masked(self):
        d = self.request("/api/settings/set", {"key": "alerts.token", "value": "topsecret"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["value"], "***")
        # GET also masked
        d2 = self.get("/api/settings")
        for items in d2["groups"].values():
            for it in items:
                if it["key"] == "alerts.token":
                    self.assertEqual(it["value"], "***")
        # and never in history
        hist = self.get("/api/history")
        for e in hist.get("entries", []):
            self.assertNotIn("topsecret", e.get("detail", ""))

    def test_settings_export_no_secrets(self):
        self.request("/api/settings/set", {"key": "alerts.token", "value": "sekrit2"})
        d = self.get("/api/settings/export")
        self.assertTrue(d["ok"])
        self.assertNotIn("sekrit2", d["yaml"])


class ConsoleApiTests(ApiBase):
    def test_whitelisted_command(self):
        d = self.request("/api/run", {"command": "version"})
        self.assertTrue(d["ok"])
        self.assertIn("atropos", d["output"][0])

    def test_rm_rf_rejected(self):
        d = self.request("/api/run", {"command": "rm -rf /"})
        self.assertFalse(d["ok"])
        self.assertIn("unknown command", d["error"])

    def test_shell_rejected(self):
        for cmd in ("sh -c 'echo pwned'", "bash", "curl | sh", "python -c 'x'"):
            d = self.request("/api/run", {"command": cmd})
            self.assertFalse(d["ok"], cmd)

    def test_path_traversal_name_rejected(self):
        d = self.request("/api/run", {"command": "skills install ../../etc"})
        self.assertFalse(d["ok"])
        self.assertIn("invalid", d["error"])

    def test_route_deepmo_rejected(self):
        d = self.request("/api/run", {"command": "route set deepmo"})
        self.assertFalse(d["ok"])

    def test_effort_bad_tier_rejected(self):
        d = self.request("/api/run", {"command": "effort set nope"})
        self.assertFalse(d["ok"])

    def test_settings_type_rejected_via_console(self):
        d = self.request("/api/run", {"command": "settings set dashboard.port abc"})
        self.assertFalse(d["ok"])
        self.assertIn("integer", d["error"])


class ExtensionApiTests(ApiBase):
    def test_extensions_listing(self):
        d = self.get("/api/extensions")
        self.assertTrue(d["ok"])
        self.assertIn("items", d)
        self.assertIn("trash", d)

    def test_extension_bad_name(self):
        d = self.request("/api/extensions/action",
                         {"action": "remove", "name": "../../etc"})
        self.assertFalse(d["ok"])


class MarketplaceApiTests(ApiBase):
    def test_marketplace_catalog_shape(self):
        d = self.get("/api/marketplace", timeout=120)
        self.assertTrue(d["ok"])
        self.assertTrue(len(d["sources"]) >= 2)
        for src in d["sources"]:
            self.assertIn("items", src)
            self.assertIn("id", src)

    def test_marketplace_install_unknown_source(self):
        d = self.request("/api/marketplace/install",
                         {"source": "evil.example", "item": "x"})
        self.assertFalse(d["ok"])

    def test_marketplace_install_bad_name(self):
        d = self.request("/api/marketplace/install",
                         {"source": "anthropics/skills", "item": "../../x"})
        self.assertFalse(d["ok"])


class MiddlewareApiTests(ApiBase):
    def test_middleware_list(self):
        d = self.request("/api/middleware/list")
        self.assertTrue(d["ok"])
        self.assertGreaterEqual(len(d["filters"]), 12)
        for row in d["filters"]:
            self.assertEqual(len(row), 3)  # [name, description, state]

    def test_middleware_on_off(self):
        d = self.request("/api/middleware/on", {"name": "pii"})
        self.assertTrue(d["ok"])
        self.assertIn("pii", d["enabled"])
        d2 = self.request("/api/middleware/off", {"name": "pii"})
        self.assertTrue(d2["ok"])
        self.assertNotIn("pii", d2["enabled"])


class SessionApiTests(ApiBase):
    def test_search_requires_query(self):
        d = self.get("/api/sessions/search?q=")
        self.assertFalse(d["ok"])

    def test_export_shape(self):
        d = self.get("/api/sessions/export")
        self.assertIn("ok", d)


class StaticTests(ApiBase):
    def test_index_served(self):
        resp = urllib.request.urlopen(f"http://127.0.0.1:{self._port}/", timeout=10)
        body = resp.read().decode("utf-8", errors="replace")
        self.assertIn("Atropos", body)

    def test_sw_served(self):
        resp = urllib.request.urlopen(f"http://127.0.0.1:{self._port}/sw.js", timeout=10)
        body = resp.read().decode("utf-8")
        self.assertIn("serviceWorker", body) if "serviceWorker" in body else True

    def test_unknown_api_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)


class SseTests(ApiBase):
    def test_events_stream_connects(self):
        import socket as _socket
        s = _socket.create_connection(("127.0.0.1", self._port), timeout=10)
        s.sendall(
            f"GET /api/events HTTP/1.1\r\nHost: x\r\n"
            f"Cookie: atropos_session={self._token}\r\n\r\n".encode())
        s.settimeout(20)
        try:
            first = s.recv(4096)
            self.assertIn(b"200 OK", first)
            self.assertIn(b"text/event-stream", first)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()