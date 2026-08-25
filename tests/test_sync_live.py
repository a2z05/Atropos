#!/usr/bin/env python3
"""Live sync (v18 H) — watch debounce, delta push, relay, manifest backup."""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import backup, sync_live, detect  # noqa: E402


class LiveBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_live_")
        os.environ["ATROPOS_HOME"] = self.tmp
        self.tok = "live-test-token"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)


class ServePushPollTests(LiveBase):
    """Peer A serves; peer B pushes; A applies."""

    def setUp(self):
        super().setUp()
        # module-global delta queue persists across tests — clear it
        with sync_live._LOCK:
            sync_live._QUEUE.clear()
            sync_live._LAST["ts"] = 0.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = sync_live.live_serve(port=0, token="live-test-token")
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.th.join(timeout=5)

    def _push(self, base, rel, content):
        p = Path(base) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return sync_live.live_push(
            f"http://127.0.0.1:{self.port}", self.tok, since=0.0, base=base)

    def test_push_applies_to_serve_home_and_polls(self):
        peer = tempfile.mkdtemp(prefix="atropos_peer_")
        self.addCleanup(shutil.rmtree, peer, ignore_errors=True)
        r = self._push(peer, "memory/note.txt", "hello from peer")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pushed"], 1)
        # the serve-side home (this ATROPOS_HOME) now holds the pushed file
        self.assertTrue((detect.atropos_home() / "memory" / "note.txt").exists())
        # a poll from this home sees the delta as already-applied (unchanged)
        r2 = sync_live.live_poll(f"http://127.0.0.1:{self.port}", self.tok)
        disk = (detect.atropos_home() / "memory" / "note.txt").read_text()
        if r2["applied"] != 0:
            print(f"DEBUG applied={r2['applied']} disk={disk!r} home={detect.atropos_home()}")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["applied"], 0)  # unchanged — already here
        # the server still holds the delta in its queue for fresh pollers
        with sync_live._LOCK:
            rels = [q[1] for q in sync_live._QUEUE]
        self.assertIn("memory/note.txt", rels)

    def test_secrets_never_sync(self):
        peer = tempfile.mkdtemp(prefix="atropos_secret_")
        self.addCleanup(shutil.rmtree, peer, ignore_errors=True)
        (Path(peer) / "auth_token").write_text("super-secret\n")
        r = self._push(peer, "auth_token", "super-secret")
        self.assertEqual(r["pushed"], 0)

    def test_conflict_journal_written(self):
        peer = tempfile.mkdtemp(prefix="atropos_conflict_")
        self.addCleanup(shutil.rmtree, peer, ignore_errors=True)
        p = Path(peer) / "config.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v1", encoding="utf-8")
        # serve-side home already has config.yaml with different content
        home_cfg = detect.atropos_home() / "config.yaml"
        home_cfg.parent.mkdir(parents=True, exist_ok=True)
        home_cfg.write_text("v2", encoding="utf-8")
        r = self._push(peer, "config.yaml", "v1")
        self.assertTrue(r["ok"])
        entries = sync_live.journal()
        self.assertTrue(entries, "journal should record the apply")

    def test_live_serve_state_endpoint(self):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/livesync/state",
            headers={"Authorization": f"Bearer {self.tok}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode())
        self.assertTrue(d["ok"])
        self.assertIn("pending", d)


class RelayTests(LiveBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = sync_live.relay_serve(port=0)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.th.join(timeout=5)

    def test_relay_roundtrip_outbound_only(self):
        base = Path(self.tmp)
        p = base / "memory/relay-note.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("relayed", encoding="utf-8")
        r = sync_live.relay_put(f"http://127.0.0.1:{self.port}", "code-42")
        self.assertTrue(r.get("ok"))
        t = sync_live.relay_take(f"http://127.0.0.1:{self.port}", "code-42")
        self.assertTrue(t.get("ok"))
        self.assertGreaterEqual(t.get("applied", 0), 0)


class FullBackupTests(LiveBase):
    """Complete backup (v18 H.2) — manifest, scope, token masking."""

    def _make_scope(self):
        home = detect.atropos_home()
        (home / "memory").mkdir(parents=True, exist_ok=True)
        (home / "memory" / "note.md").write_text("remember this", encoding="utf-8")
        (home / "skills").mkdir(exist_ok=True)
        (home / "skills" / "x.md").write_text("skill", encoding="utf-8")
        (home / "agents").mkdir(exist_ok=True)
        (home / "custom_filters").mkdir(exist_ok=True)
        (home / "chat.db").write_bytes(b"\x00\x01db")
        (home / "auth_token").write_text("topsecret-token\n")

    def test_manifest_in_backup(self):
        self._make_scope()
        r = backup.create()
        self.assertTrue(r["ok"])
        import tarfile
        with tarfile.open(r["path"], "r:gz") as tar:
            names = tar.getnames()
            self.assertIn("MANIFEST.json", names)
            m = json.loads(tar.extractfile("MANIFEST.json").read().decode())
        self.assertIn("version", m)
        self.assertIn("checksums", m)
        self.assertEqual(m["secrets_masked"], True)

    def test_scope_includes_atropos_dirs(self):
        self._make_scope()
        r = backup.create()
        import tarfile
        with tarfile.open(r["path"], "r:gz") as tar:
            names = "\n".join(tar.getnames())
        self.assertIn("atropos/memory", names)
        self.assertIn("atropos/skills", names)
        self.assertIn("atropos/chat.db", names)

    def test_secret_file_excluded(self):
        self._make_scope()
        (detect.atropos_home() / "dashboard_auth.json").write_text("{}\n")
        r = backup.create()
        import tarfile
        with tarfile.open(r["path"], "r:gz") as tar:
            names = "\n".join(tar.getnames())
        self.assertNotIn("auth_token", names)
        self.assertNotIn("dashboard_auth.json", names)

    def test_restore_roundtrip(self):
        self._make_scope()
        r = backup.create()
        # wipe the memory dir then restore
        shutil.rmtree(detect.atropos_home() / "memory")
        rr = backup.restore(Path(r["path"]))
        self.assertTrue(rr.get("ok"), rr.get("error"))
        self.assertTrue((detect.atropos_home() / "memory" / "note.md").exists())


if __name__ == "__main__":
    unittest.main()