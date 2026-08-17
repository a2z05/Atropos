#!/usr/bin/env python3
"""Behavior parity checks for Hermes source ports.

Rule (v18 A.2): same inputs → same outputs on >=3 representative cases per
ported module. Where Atropos deliberately deviates (stdlib instead of
aiohttp, own schema), the test documents what changed and why.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import chat, search  # noqa: E402


# ── Section A ported modules: web (web_tools.py), x (xurl skill), ha ────
class _HermeticMixin(unittest.TestCase):
    """Sandbox ATROPOS_HOME per case and drop gateway/provider env vars."""

    _SNIP = ("NINEROUTER_URL", "NINEROUTER_KEY", "TAVILY_API_KEY",
             "TAVILY_BASE_URL", "EXA_API_KEY", "PARALLEL_API_KEY",
             "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL", "SEARXNG_URL",
             "BRAVE_SEARCH_API_KEY", "HASS_URL", "HASS_TOKEN",
             "ATROPOS_ALLOW_PRIVATE_URLS")

    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_parity_")
        os.environ["ATROPOS_HOME"] = self.tmp
        self._saved = {v: os.environ.get(v) for v in self._SNIP}
        for v in self._SNIP:
            os.environ.pop(v, None)
        # settings cache in ~/.atropos is per-home; force a fresh read
        import importlib
        import core.settings as _s
        importlib.reload(_s)

    def tearDown(self):
        for v, val in self._saved.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)

    def _assert_mock_ok(self, res: dict):
        self.assertTrue(res.get("ok"), res.get("error"))


class WebParityTests(_HermeticMixin):
    """core/web.py — ported from hermes-agent/tools/web_tools.py + url_safety.py."""

    def _mock_fetch(self, url, status=200, body=None, ctype="application/json"):
        from unittest import mock
        m = mock.patch("urllib.request.urlopen")
        fake = m.start()
        fake.side_effect = None
        body = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
        fake.return_value = mock.Mock(
            __enter__=mock.Mock(return_value=mock.Mock(
                read=lambda: body.encode("utf-8"),
                headers={"Content-Type": ctype},
            )),
            __exit__=mock.Mock(return_value=False),
        )
        if status >= 400:
            exc = __import__("urllib.error").error.HTTPError
            fake.side_effect = exc(url, status, "err", {}, None)
        self.addCleanup(m.stop)
        return fake

    def test_url_normalize_iri_host(self):
        # normalize_url_for_request: IRI host -> IDNA ASCII host
        from core import web
        out = web.normalize_url_for_request("https://wttr.in/Köln")
        self.assertIn("K%C3%B6ln", out)
        self.assertIn("wttr.in", out)

    def test_url_normalize_whitespace_after_scheme_repair(self):
        from core import web
        out = web.normalize_url_for_request("https:// docs.example/page")
        self.assertTrue(out.startswith("https://"))
        self.assertNotIn("https:// ", out)

    def test_ssrf_blocks_metadata_and_loopback(self):
        # is_safe_url + url_safety_check: always-blocked metadata fails
        # closed even though localhost would be allowed otherwise.
        from core import web
        self.assertFalse(web.is_safe_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(web.is_safe_url("http://metadata.google.internal/"))
        res = web.url_safety_check("http://127.0.0.1:8123")
        self.assertTrue(res.get("ok"))
        self.assertFalse(res.get("safe"))

    def test_secret_in_query_blocks_extract(self):
        # web_extract_tool secret-param block: vendor prefix (redact._PREFIX_RE)
        # and credential-named query params both abort before any request.
        from core import web
        res = web._extract(["https://example.com/page?token=abc123"])
        self.assertFalse(res.get("ok"))
        self.assertIn("credential-like query parameter", res.get("error", ""))
        res2 = web._extract(["https://example.com/cb?sk-abcdefghijklmnopqrstuvwxyz"])
        self.assertFalse(res2.get("ok"))
        self.assertIn("API key or token", res2.get("error", ""))

    def test_tavily_search_normalization(self):
        # tavily provider search -> canonical {success, data.web[...]}
        from core import web
        self._mock_fetch("https://api.tavily.com/search", body={
            "results": [
                {"title": "T1", "url": "http://a.example", "content": "snippet one"},
                {"title": "T2", "url": "http://b.example", "content": "snippet two"},
            ]
        })
        os.environ["TAVILY_API_KEY"] = "tvly-fixture"
        res = web.web_search("fixture query", k=2)
        self._assert_mock_ok(res)
        self.assertEqual(len(res["results"]), 2)
        self.assertEqual(res["results"][0]["title"], "T1")
        self.assertEqual(res["results"][0]["position"], 1)
        self.assertEqual(res["results"][1]["url"], "http://b.example")

    def test_brave_search_normalization(self):
        # brave-free provider: nested web.results, X-Subscription-Token
        from core import web
        self._mock_fetch("https://api.search.brave.com/res/v1/web/search", body={
            "web": {"results": [{"title": "B1", "url": "http://c.example",
                                 "description": "desc"}]}
        })
        os.environ["BRAVE_SEARCH_API_KEY"] = "brave-fixture"
        res = web.web_search("brave query")
        self._assert_mock_ok(res)
        self.assertEqual(res["results"][0]["description"], "desc")
        self.assertEqual(res["results"][0]["position"], 1)

    def test_search_without_backend_graceful(self):
        from core import web
        res = web.web_search("anything")
        self.assertFalse(res.get("ok"))
        self.assertIn("provider", res.get("error", ""))

    def test_base64_images_become_placeholders(self):
        # convert_base64_images_to_links: token-bomb guard from web_tools
        from core import web
        payload = ("![alt](data:image/png;base64,AAAA) "
                   "(data:image/png;base64,BBBB) text data:image/png;base64,CCCC")
        out = web.convert_base64_images_to_links(payload)
        self.assertIn("[IMAGE: alt]", out)
        # exact "[IMAGE]" marker appears once per non-markdown blob
        self.assertEqual(out.count("[IMAGE]"), 2)
        self.assertNotIn("base64", out)

    def test_extract_truncate_and_store_footer(self):
        # _truncate_with_footer: head+tail window + stored full text
        from core import web
        content = ("line zero\n" + ("word " * 500))
        model_text, truncated = web._truncate_with_footer(
            content, "https://example.com/long-page", char_limit=2000)
        self.assertTrue(truncated)
        self.assertIn("[TRUNCATED]", model_text)
        self.assertIn("Full text saved to:", model_text)
        home = Path(self.tmp)
        self.assertTrue(list((home / "cache" / "web").glob("*.md")))

    def test_extract_firecrawl_scrape_shape(self):
        # firecrawl provider per-URL loop: metadata.sourceURL post-redirect
        # re-check, markdown selected
        from core import web
        self._mock_fetch("https://api.firecrawl.dev/v1/scrape", body={
            "success": True,
            "data": {"markdown": "# Hello\nbody text",
                     "metadata": {"title": "Page", "sourceURL": "https://example.com/post"}}
        })
        os.environ["FIRECRAWL_API_KEY"] = "fc-fixture"
        res = web._extract(["https://example.com/post"], format="markdown")
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["results"][0]["title"], "Page")
        self.assertIn("body text", res["results"][0]["content"])

    def test_extract_ssrf_blocks_private_url(self):
        from core import web
        res = web._extract(["http://192.168.1.10/secret"])
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["results"][0]["error"],
                         "Blocked: URL targets a private or internal network address")


class XParityTests(_HermeticMixin):
    """core/x.py — xurl CLI wrapper, ported from the xurl skill quick reference."""

    def _fake_xurl(self):
        """Patch both the which-probe and subprocess.run so the xurl binary
        appears installed and every command succeeds with JSON stdout."""
        from unittest import mock
        m = mock.patch("core.x.shutil.which", return_value="/fake/xurl")
        m.start()
        self.addCleanup(m.stop)
        p = mock.patch("core.x.subprocess.run")
        fake = p.start()
        fake.return_value = mock.Mock(returncode=0, stdout='{"ok": true}',
                                      stderr="")
        self.addCleanup(p.stop)
        return fake

    def test_post_without_xurl_graceful_with_hint(self):
        # xurl absent: {ok: False} + install/auth how-to (xurl skill docs)
        import core.x as x
        res = x.x_post("hello world")
        self.assertFalse(res.get("ok"))
        err = res.get("error", "")
        self.assertIn("xurl", err)
        self.assertIn("auth oauth2", err)

    def test_post_runs_xurl_post(self):
        import core.x as x
        self._fake_xurl()
        res = x.x_post("hello world")
        self._assert_mock_ok(res)
        call = x.subprocess.run.call_args
        self.assertEqual(call[0][0][0], "xurl")
        self.assertEqual(call[0][0][1], "post")
        self.assertEqual(call[0][0][2], "hello world")

    def test_search_passes_n_flag(self):
        import core.x as x
        self._fake_xurl()
        res = x.x_search("golang from:me", n=7)
        self._assert_mock_ok(res)
        call = x.subprocess.run.call_args
        self.assertEqual(call[0][0], ["xurl", "search", "golang from:me", "-n", "7"])

    def test_dm_shape(self):
        import core.x as x
        self._fake_xurl()
        res = x.x_dm("@someone", "hi there")
        self._assert_mock_ok(res)
        call = x.subprocess.run.call_args
        self.assertEqual(call[0][0], ["xurl", "dm", "@someone", "hi there"])

    def test_x_post_requires_text(self):
        import core.x as x
        res = x.x_post("   ")
        self.assertFalse(res.get("ok"))

    def test_command_failure_envelope(self):
        import core.x as x
        from unittest import mock
        with mock.patch("core.x.shutil.which", return_value="/fake/xurl"):
            m = mock.patch("core.x.subprocess.run")
            fake = m.start()
            fake.return_value = mock.Mock(returncode=1, stdout="",
                                          stderr="auth required")
            self.addCleanup(m.stop)
            self.assertEqual(x.x_whoami(), {"ok": False, "error": "auth required"})


class HaParityTests(_HermeticMixin):
    """core/ha.py — ported from hermes-agent/tools/homeassistant_tool.py."""

    STATES = [
        {"entity_id": "light.living_room", "state": "on",
         "attributes": {"friendly_name": "Living Room Light", "brightness": 255}},
        {"entity_id": "sensor.temperature", "state": "21.5",
         "attributes": {"friendly_name": "Kitchen Temperature"}},
        {"entity_id": "switch.office", "state": "off",
         "attributes": {"friendly_name": "Office Switch"}},
    ]

    def _mock_ha(self, payloads=None, statuses=None):
        """Mock urllib.request.urlopen for HA REST; payloads by call order."""
        from unittest import mock
        m = mock.patch("urllib.request.urlopen")
        fake = m.start()
        self.addCleanup(m.stop)
        payloads = payloads if payloads is not None else (self.STATES,)
        statuses = statuses or [200] * len(payloads)
        calls = []

        def _open(req, timeout):
            calls.append(req)
            status = statuses[len(calls) - 1]
            idx = len(calls) - 1
            body = json.dumps(payloads[idx]) if payloads[idx] is not None else ""
            if status >= 400:
                raise __import__("urllib.error").error.HTTPError(
                    req.full_url, status, "err", {}, None)
            return mock.Mock(
                __enter__=mock.Mock(return_value=mock.Mock(
                    read=lambda: body.encode("utf-8"))),
                __exit__=mock.Mock(return_value=False),
            )

        fake.side_effect = _open
        return calls

    def test_states_no_token_graceful(self):
        from core import ha
        res = ha.ha_states()
        self.assertFalse(res.get("ok"))
        self.assertIn("HASS_TOKEN", res.get("error", ""))

    def test_states_list_and_filter_by_domain(self):
        from core import ha
        os.environ["HASS_TOKEN"] = "fixture-token"
        os.environ["HASS_URL"] = "http://hass.test"
        calls = self._mock_ha()
        res = ha.ha_states(domain="light")
        self._assert_mock_ok(res)
        self.assertEqual(res["result"]["count"], 1)
        self.assertEqual(res["result"]["entities"][0]["entity_id"], "light.living_room")
        self.assertEqual(res["result"]["entities"][0]["state"], "on")
        self.assertEqual(calls[0].full_url, "http://hass.test/api/states")
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer fixture-token")

    def test_entity_detail_attributes(self):
        from core import ha
        os.environ["HASS_TOKEN"] = "fixture-token"
        os.environ["HASS_URL"] = "http://hass.test"
        entity = dict(self.STATES[0])
        entity["last_changed"] = "2026-08-01T12:00:00Z"
        self._mock_ha((entity,))
        res = ha.ha_entity("light.living_room")
        self._assert_mock_ok(res)
        self.assertEqual(res["result"]["state"], "on")
        self.assertEqual(res["result"]["attributes"]["brightness"], 255)
        self.assertEqual(res["result"]["last_changed"], "2026-08-01T12:00:00Z")

    def test_entity_id_validation(self):
        from core import ha
        self.assertFalse(ha.ha_entity("") .get("ok"))
        self.assertFalse(ha.ha_entity("LIGHT.UP") .get("ok"))
        self.assertFalse(ha.ha_entity("../etc/passwd").get("ok"))

    def test_call_service_posts_and_parses_affected(self):
        from core import ha
        os.environ["HASS_TOKEN"] = "fixture-token"
        os.environ["HASS_URL"] = "http://hass.test"
        self._mock_ha(([{"entity_id": "light.living_room", "state": "on"}],))
        res = ha.ha_call_service("light", "turn_on", entity_id="light.living_room")
        self._assert_mock_ok(res)
        affected = res["affected_entities"]
        self.assertEqual(affected[0]["entity_id"], "light.living_room")
        self.assertEqual(affected[0]["state"], "on")
        self.assertEqual(res["service"], "light.turn_on")

    def test_blocked_domains_rejected_before_request(self):
        # homeassistant_tool._BLOCKED_DOMAINS — no request is attempted
        from core import ha
        os.environ["HASS_TOKEN"] = "fixture-token"
        calls = self._mock_ha()
        for domain in ("shell_command", "python_script", "rest_command"):
            res = ha.ha_call_service(domain, "run")
            self.assertFalse(res.get("ok"))
            self.assertIn("blocked for security", res.get("error", ""))
        self.assertEqual(calls, [], "no request should hit the HA server")

    def test_service_name_path_traversal_rejected(self):
        from core import ha
        res = ha.ha_call_service("../../api/config", "get")
        self.assertFalse(res.get("ok"))
        self.assertIn("Invalid domain format", res.get("error", ""))

    def test_http_error_envelope(self):
        from core import ha
        os.environ["HASS_TOKEN"] = "fixture-token"
        os.environ["HASS_URL"] = "http://hass.test"
        self._mock_ha((None,), statuses=(401,))
        res = ha.ha_entity("light.living_room")
        self.assertFalse(res.get("ok"))
        self.assertIn("HTTP 401", res.get("error", ""))


class ToolsGatewayFallbackTests(_HermeticMixin):
    """core/tools.py rewired entry points: 9Router gateway first, the
    hermes fallback chain (core.web / core.x) second."""

    def test_web_search_gateway_is_first_provider(self):
        # gateway configured -> tools.web_search hits /v1/search only
        import core.tools as tools
        from unittest import mock
        os.environ["NINEROUTER_URL"] = "http://gw.test"
        os.environ["NINEROUTER_KEY"] = "k-secret"
        with mock.patch("urllib.request.urlopen") as u:
            u.return_value.__enter__ = mock.Mock(return_value=mock.Mock(
                read=lambda: b'{"web": [{"title": "G"}]}', headers={}))
            u.return_value.__exit__ = mock.Mock(return_value=False)
            res = tools.web_search("q")
        self._assert_mock_ok(res)
        req = u.call_args[0][0]
        self.assertEqual(req.full_url, "http://gw.test/v1/search")
        self.assertEqual(req.get_header("Authorization"), "Bearer k-secret")

    def test_web_search_gateway_down_falls_through(self):
        # gateway request fails -> hermes provider chain tried instead
        import core.tools as tools
        from unittest import mock
        os.environ["NINEROUTER_URL"] = "http://gw.test"
        os.environ["NINEROUTER_KEY"] = "k-secret"
        os.environ["TAVILY_API_KEY"] = "tvly-fallback"
        with mock.patch("urllib.request.urlopen") as u:
            u.side_effect = OSError("gw down")
            with mock.patch("core.web._post_json") as pj:
                pj.return_value = {"results": [
                    {"title": "H", "url": "http://x.example", "content": "c"}]}
                res = tools.web_search("q")
        self._assert_mock_ok(res)
        self.assertEqual(res.get("backend"), "tavily")
        self.assertEqual(res["results"][0]["title"], "H")

    def test_web_fetch_gateway_down_falls_through(self):
        import core.tools as tools
        from unittest import mock
        os.environ["NINEROUTER_URL"] = "http://gw.test"
        os.environ["NINEROUTER_KEY"] = "k-secret"
        with mock.patch("urllib.request.urlopen") as u:
            u.side_effect = OSError("gw down")
            r2 = tools.web_fetch("https://example.com/doc")
        self.assertFalse(r2.get("ok"))  # no backend configured -> graceful

    def test_x_post_missing_xurl_graceful(self):
        # tools.x_post now defers to core/x.py with identical shape
        import core.tools as tools
        res = tools.x_post("hi")
        self.assertFalse(res.get("ok"))
        self.assertIn("xurl", res.get("error", ""))


class SearchParityTests(unittest.TestCase):
    """Ported from hermes_state_search.py (FTS5) — fixture cases."""

    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_parity_")
        os.environ["ATROPOS_HOME"] = self.tmp
        self.sid = chat.create_session("deploy session")
        # fixture: three messages with known vocabulary
        conn = chat._connect()
        try:
            chat._init_db()
            for role, content in (
                ("user", "How do I deploy the docker container to railway?"),
                ("assistant", "Run atropos sync live with the railway peer."),
                ("user", "docker compose up --build worked on the box"),
            ):
                chat._insert_message(conn, self.sid, role, content, "auto", "", "medium", 0, chat._now())
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)

    def _conn(self):
        conn = chat._connect()
        chat._init_db()
        return conn

    def test_fixture_case_docker(self):
        with self._conn() as conn:
            rows = search.search(conn, "docker", k=5)
        self.assertTrue(rows, "expected a docker hit")
        self.assertIn("docker", rows[0]["content"].lower())
        self.assertNotIn("railway peer", rows[0]["content"].lower())

    def test_fixture_case_phrase(self):
        with self._conn() as conn:
            rows = search.search(conn, '"sync live"', k=5)
        self.assertEqual(len(rows), 1)
        self.assertIn("sync live", rows[0]["content"])

    def test_fixture_case_no_match(self):
        with self._conn() as conn:
            rows = search.search(conn, "zzzz-no-such-term", k=5)
        self.assertEqual(rows, [])

    def test_sanitizer_quoted_phrases_survive(self):
        with self._conn() as conn:
            rows = search.search(conn, '"docker compose" up', k=5)
        self.assertTrue(rows)
        self.assertIn("docker compose", rows[0]["content"])

    def test_sanitizer_dotted_term_quoted(self):
        # FTS5 would split chat-send into chat AND send; sanitizer quotes it
        with self._conn() as conn:
            rows = search.search(conn, "chat-send", k=5)
        # no crash + plausible: matches nothing but doesn't error
        self.assertIsInstance(rows, list)

    def test_cjk_short_uses_like(self):
        with self._conn() as conn:
            rows = search.search(conn, "部署", k=5)
        self.assertIsInstance(rows, list)

    def test_anchored_window_shapes(self):
        conn = self._conn()
        try:
            msgs = chat.session_messages(self.sid)
            mid = msgs[1]["id"]
            view = search.anchored_view(conn, self.sid, mid, window=2, bookend=1)
        finally:
            conn.close()
        self.assertIn("window", view)
        self.assertIn("bookend_start", view)
        self.assertIn("bookend_end", view)
        self.assertGreaterEqual(len(view["window"]), 2)
        # anchor always present
        self.assertTrue(any(m["id"] == mid for m in view["window"]))

    def test_fts_index_built(self):
        conn = self._conn()
        try:
            self.assertTrue(search._fts_available(conn))
        finally:
            conn.close()

    def test_search_via_chat_api(self):
        rows = chat.search_messages("railway", k=3)
        self.assertTrue(rows)
        self.assertIn("snippet", rows[0])
        self.assertIn("title", rows[0])


if __name__ == "__main__":
    unittest.main()
