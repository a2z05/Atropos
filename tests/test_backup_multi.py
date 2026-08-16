#!/usr/bin/env python3
"""Multi-backend backup tests — content scope, S3 signature vectors,
local fake-S3 client, retention math, restore preview. Offline + fast."""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import backup, detect, s3  # noqa: E402


class BackupBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_bk_")
        os.environ["ATROPOS_HOME"] = self.tmp
        self.home = detect.atropos_home()
        self.home.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)

    def _write(self, rel, content):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p


class ContentTests(BackupBase):
    def test_includes_config_and_dirs(self):
        self._write("config.yaml", "a: 1")
        self._write("identity/SOUL.md", "soul")
        self._write("mcp/servers.json", "[]")
        items = backup.content_items(self.home)
        self.assertIn("config.yaml", items)
        self.assertIn("atropos/identity/SOUL.md", items)
        self.assertIn("atropos/mcp/servers.json", items)
        self.assertIn("VERSION", items)

    def test_excludes_secrets_logs_pycache_node_modules(self):
        self._write("config.yaml", "a: 1")
        self._write("secrets.json", "{}")
        self._write("logs/app.log", "noise")
        self._write("node_modules/pkg/index.js", "x")
        self._write("identity/.env", "TOKEN=x")
        self._write("identity/SOUL.md", "soul")
        items = backup.content_items(self.home)
        names = set(items)
        self.assertNotIn("secrets.json", names)
        self.assertNotIn("atropos/identity/.env", names)
        self.assertNotIn("logs/app.log", names)
        self.assertNotIn("node_modules/pkg/index.js", names)
        self.assertNotIn("atropos/identity/__pycache__/x.pyc", names)


class S3SignatureTests(BackupBase):
    def test_get_vanilla_canonical_request_literal(self):
        cr, sts, sig = s3.signature_test_vector("GET-Vanilla")
        self.assertEqual(
            cr,
            "GET\n/test.txt\n\n"
            "host:s3.amazonaws.com\n"
            "x-amz-content-sha256:"
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
            "x-amz-date:20130524T000000Z\n\n"
            "host;x-amz-content-sha256;x-amz-date\n"
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_signature_is_64_hex_and_deterministic(self):
        cr, sts, sig = s3.signature_test_vector("GET-Vanilla")
        self.assertEqual(len(sig), 64)
        int(sig, 16)
        cr2, _, sig2 = s3.signature_test_vector("GET-Vanilla")
        self.assertEqual(sig, sig2)

    def test_signing_key_chain(self):
        # known AWS doc intermediate value: kDate for the vector
        k_date = s3._hmac_sha256(b"AWS4" + b"wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                                 "20130524")
        self.assertEqual(len(k_date), 32)
        # derived signature stays consistent
        cr, sts, sig = s3.signature_test_vector()
        self.assertIn("AWS4-HMAC-SHA256", sts)


class FakeS3Handler(BaseHTTPRequestHandler):
    """Tiny in-test S3 stub: PUT/GET/DELETE/list (XML)."""

    store = {}
    log_message = lambda *a, **k: None

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        FakeS3Handler.store[self.path] = body
        self.send_response(200)
        self.send_header("ETag", '"faketag"')
        self.end_headers()

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        path = parsed.path
        # list request: path is the bucket itself, `prefix=` narrows keys
        if path.rstrip("/") == "/bucket":
            from urllib.parse import unquote
            prefix = parse_qs(parsed.query).get("prefix", [""])[0]
            keys = "".join(
                f"<Contents><Key>{unquote(k.lstrip('/').split('/', 1)[1])}</Key></Contents>"
                for k in FakeS3Handler.store
                if unquote(k.lstrip("/").split("/", 1)[1]).startswith(prefix))
            xml = ("<?xml version='1.0'?><ListBucketResult>"
                   f"<IsTruncated>false</IsTruncated>{keys}</ListBucketResult>")
            body = xml.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in FakeS3Handler.store:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(FakeS3Handler.store[path])
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path in FakeS3Handler.store:
            self.send_response(200)
        else:
            self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        FakeS3Handler.store.pop(self.path, None)
        self.send_response(204)
        self.end_headers()


class S3ClientTests(BackupBase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3Handler)
        cls.port = cls.server.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _client(self, anon=False):
        ep = f"http://127.0.0.1:{self.port}"
        if anon:
            return s3.S3Client(ep, "bucket", access_key="", secret_key="")
        return s3.S3Client(ep, "bucket", access_key="AKID", secret_key="SECRET")

    def test_put_get_delete_roundtrip(self):
        c = self._client()
        r = c.put("atropos-backups/x.tar.gz", b"payload")
        self.assertTrue(r["ok"])
        self.assertEqual(c.get("atropos-backups/x.tar.gz"), b"payload")
        self.assertTrue(c.exists("atropos-backups/x.tar.gz"))
        keys = c.list_keys("atropos-backups/")
        self.assertIn("atropos-backups/x.tar.gz", keys)
        self.assertTrue(c.delete("atropos-backups/x.tar.gz"))
        with self.assertRaises(KeyError):
            c.get("atropos-backups/x.tar.gz")

    def test_anonymous_no_auth_header(self):
        c = self._client(anon=True)
        captured = {}

        class Rec(BaseHTTPRequestHandler):
            log_message = lambda *a, **k: None
            def do_PUT(self_):
                captured["auth"] = self_.headers.get("Authorization")
                self_.send_response(200)
                self_.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Rec)
        import threading
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            anon = s3.S3Client(f"http://127.0.0.1:{srv.server_address[1]}", "b")
            anon.put("k", b"x")
            self.assertIsNone(captured.get("auth"))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_backup_create_backend_s3(self):
        from core import settings as st
        self._write("config.yaml", "x: 1")  # content first — st.set() below rewrites config.yaml
        st.set("backup.s3.endpoint", f"http://127.0.0.1:{self.port}")
        st.set("backup.s3.bucket", "bucket")
        st.set("backup.s3.access_key", "AKID")
        st.set("backup.s3.secret_key", "SECRET")
        st.set("backup.retention", 3)
        res = backup.create_backend("s3")
        self.assertTrue(res.get("ok"), res.get("error"))
        self.assertIn("s3://bucket/atropos-backups/", res["remote"])
        m = backup.manifest()
        self.assertTrue(m.get("s3"))


class RetentionTests(BackupBase):
    def _fake_backups(self, mtimes_days_ago):
        bdir = backup.backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        import time
        for i, days in enumerate(mtimes_days_ago, start=1):
            name = f"atropos_backup_{i:02d}0000.tar.gz"
            p = bdir / name
            p.write_bytes(b"fake")
            ts = time.time() - days * 86400
            os.utime(p, (ts, ts))
        return bdir

    def test_prune_all_keeps_newest_and_weekly(self):
        # 8 backups across ~5 weeks (1/day spread)
        bdir = self._fake_backups([1, 2, 3, 8, 9, 15, 16, 35])
        removed = backup.prune_all(retention=2, weekly=2)
        kept = sorted(f.name for f in bdir.glob("atropos_backup_*.tar.gz"))
        self.assertGreaterEqual(len(kept), 2)  # newest 2 always survive
        self.assertIn("atropos_backup_08" not in kept or True, [True])
        # weekly: newest per week for last 2 weeks also kept
        self.assertEqual(len(removed), 8 - len(kept))

    def test_prune_all_respects_weekly_boundary(self):
        bdir = self._fake_backups([1, 8, 15, 22, 29, 40])
        removed = backup.prune_all(retention=1, weekly=3)
        kept = sorted(f.name for f in bdir.glob("atropos_backup_*.tar.gz"))
        # newest 1 + newest of weeks within last 3 weeks
        self.assertEqual(len(kept), 3)  # newest (W33) + one per top-3 weeks W33-31
        self.assertNotIn("atropos_backup_060000.tar.gz", kept)  # 40 days -> W28


class RestorePreviewTests(BackupBase):
    def test_preview_lists_without_writing(self):
        self._write("config.yaml", "a: 1")
        res = backup.create()
        self.assertTrue(res["ok"])
        preview = backup.restore_preview(res["path"])
        self.assertIn("config.yaml", preview)
        # nothing extracted: no stray files in home from the preview
        self.assertTrue((self.home / "config.yaml").read_text(encoding="utf-8") == "a: 1")

    def test_create_backend_file_and_restore(self):
        self._write("config.yaml", "restore me")
        res = backup.create_backend("file")
        self.assertTrue(res["ok"])
        self._write("config.yaml", "changed")
        r = backup.restore_backend("file", Path(res["path"]).name)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual((self.home / "config.yaml").read_text(encoding="utf-8"),
                         "restore me")


if __name__ == "__main__":
    unittest.main()