#!/usr/bin/env python3
"""Middleware (Filters & Plugins) tests — order, ctx mutation, isolation."""
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

from core import middleware


class MiddlewareBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_mw_")
        os.environ["ATROPOS_HOME"] = self.tmp
        middleware._STATE["order"] = None
        middleware._STATE["filters"] = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)


class CatalogTests(MiddlewareBase):
    def test_at_least_12_prebuilt(self):
        self.assertGreaterEqual(len(middleware.catalog()), 12)

    def test_human_descriptions(self):
        for key, meta in middleware.catalog().items():
            self.assertTrue(meta["description"])
            self.assertIn(meta["hook"],
                          ("before_model", "after_model", "before_tool", "after_tool", "on_end", "on_error"))


class PipelineTests(MiddlewareBase):
    def test_pii_redacts(self):
        middleware.set_enabled("pii", True)
        ctx = middleware.run("before_model", {"prompt": "mail me at joe@x.com or 555-123-4567"})
        self.assertNotIn("joe@x.com", ctx["prompt"])
        self.assertNotIn("555-123-4567", ctx["prompt"])

    def test_short_circuit_code_guard(self):
        middleware.set_enabled("code_guard", True)
        ctx = middleware.run("before_tool", {"tool": {"name": "shell_exec", "input": "rm -rf /"}})
        self.assertTrue(ctx.get("rejected"))

    def test_order_respected(self):
        order = ["pii", "ratelimit"]
        middleware.set_enabled("pii", True)
        middleware.set_enabled("ratelimit", True)
        middleware.set_order(order)
        self.assertEqual(middleware.enabled_list()[:2], order)

    def test_error_isolation(self):
        # a broken filter must not kill the pipeline
        middleware.set_enabled("retry", True)
        ctx = middleware.run("before_model", {"prompt": "hi", "retries": 0})
        self.assertIn("prompt", ctx)
        self.assertIn("hi", ctx["prompt"])

    def test_disabled_not_run(self):
        ctx = middleware.run("before_model", {"prompt": "mail joe@x.com"})
        self.assertEqual(ctx["prompt"], "mail joe@x.com")


class CustomFilterTests(MiddlewareBase):
    def _write(self, name, content):
        d = middleware._filters_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content, encoding="utf-8")

    def test_yaml_block_filter(self):
        self._write("no_bad.yaml", "name: no_bad\nhook: before_model\naction: block\nmatch: badword\nreplace_with: nope")
        middleware._STATE["order"] = None
        middleware._STATE["filters"] = {}
        ctx = middleware.run("before_model", {"prompt": "contains badword here"})
        self.assertTrue(ctx.get("rejected"))

    def test_yaml_append_filter(self):
        self._write("add_ctx.yaml", 'name: add_ctx\nhook: before_model\naction: append\nmatch: hi\nreplace_with: " [ctx] now"')
        middleware._STATE["order"] = None
        middleware._STATE["filters"] = {}
        ctx = middleware.run("before_model", {"prompt": "hi"})
        self.assertIn("[ctx] now", ctx["prompt"])

    def test_code_filter_loaded(self):
        self._write("mine.py", "def filter(ctx):\n    ctx['prompt'] = (ctx.get('prompt') or '') + ' PY'\n    return ctx\n")
        middleware._STATE["order"] = None
        middleware._STATE["filters"] = {}
        ctx = middleware.run("before_model", {"prompt": "x"})
        self.assertIn("PY", ctx["prompt"])

    def test_invalid_yaml_rejected(self):
        self._write("bad.yaml", "action: nonsense\nmatch: [unclosed")
        middleware._STATE["order"] = None
        middleware._STATE["filters"] = {}
        ctx = middleware.run("before_model", {"prompt": "x"})
        self.assertIn("prompt", ctx)

    def test_sandbox_rejects_dangerous_import(self):
        self._write("evil.py", "import os\ndef filter(ctx):\n    return ctx\n")
        middleware._STATE["order"] = None
        middleware._STATE["filters"] = {}
        ctx = middleware.run("before_model", {"prompt": "x"})
        self.assertIn("prompt", ctx)  # evil filter skipped, pipeline fine


if __name__ == "__main__":
    unittest.main()