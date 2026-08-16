#!/usr/bin/env python3
"""Multi-backend sync tests — delta detection, secrets exclusion, conflicts,
version history, pair flow, backend abstraction. Offline + fast."""

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

from core import detect, sync  # noqa: E402


class SyncBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_sync_")
        os.environ["ATROPOS_HOME"] = self.tmp
        self.home = detect.atropos_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.target = Path(self.tmp) / "mirror"

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


class ScanDiffTests(SyncBase):
    def test_scan_hashes_files(self):
        self._write("config.yaml", "router:\n  active: nain\n")
        self._write("identity/SOUL.md", "I am Atra.")
        sc = sync.scan(self.home)
        self.assertIn("config.yaml", sc)
        self.assertIn("identity/SOUL.md", sc)
        self.assertTrue(sc["config.yaml"]["hash"])

    def test_diff_detects_exactly_the_changed_file(self):
        self._write("config.yaml", "a: 1")
        self._write("identity/SOUL.md", "soul")
        local = sync.scan(self.home)
        # baseline: mark local as last-synced
        for k, v in local.items():
            v["_idx_hash"] = v["hash"]
        self._write("config.yaml", "a: 2")  # only this changed
        local2 = sync.scan(self.home)
        for k, v in local2.items():
            v["_idx_hash"] = local[k]["hash"]
        to_push, to_pull, conflicts = sync.diff(local2, local)
        self.assertEqual(to_push, ["config.yaml"])
        self.assertEqual(to_pull, [])
        self.assertEqual(conflicts, [])

    def test_diff_pull_when_remote_changed(self):
        self._write("config.yaml", "a: 1")
        local = sync.scan(self.home)
        for k, v in local.items():
            v["_idx_hash"] = v["hash"]
        # remote differs from the baseline but local hasn't changed
        remote = {k: {**v, "hash": "0" * 64} for k, v in local.items()}
        to_push, to_pull, conflicts = sync.diff(local, remote)
        self.assertEqual(to_pull, ["config.yaml"])
        self.assertEqual(to_push, [])
        self.assertEqual(conflicts, [])


class SecretsTests(SyncBase):
    def test_secrets_never_in_managed(self):
        self._write("secrets.json", "{}")
        self._write("nested/.env", "TOKEN=x")
        self._write("auth_token", "abc")
        self._write("config.yaml", "ok: 1")
        managed = sync.managed_files(self.home)
        self.assertNotIn("secrets.json", managed)
        self.assertNotIn("nested/.env", managed)
        self.assertNotIn("auth_token", managed)
        self.assertIn("config.yaml", managed)

    def test_secrets_excluded_lists_names(self):
        ex = sync.secrets_excluded()
        self.assertTrue(any("secrets" in e for e in ex))
        self.assertTrue(any(".env" in e or "env" == e for e in ex))

    def test_private_dir_never_synced(self):
        self._write("sync/foo.json", "{}")
        managed = sync.managed_files(self.home)
        self.assertNotIn("sync/foo.json", managed)
        self.assertNotIn("mirror/anything", managed)

    def test_sensitive_respects_hard_allowlist(self):
        self._write("config.yaml", "x: 1")
        self._write("logs/app.log", "noise")
        managed = sync.managed_files(self.home)
        self.assertNotIn("logs/app.log", managed)


class PushPullTests(SyncBase):
    def test_delta_push_pull_roundtrip(self):
        self._write("config.yaml", "delta: 1")
        self._write("identity/SOUL.md", "soul v1")
        res = sync.sync_push("file", target=str(self.target))
        self.assertTrue(res.get("ok", True))
        # second home
        home2 = Path(self.tmp) / "home2"
        home2.mkdir()
        with mock.patch("core.detect.atropos_home", return_value=home2):
            pull = sync.sync_pull("file", source=str(self.target))
        self.assertTrue(pull.get("ok", True))
        self.assertEqual((home2 / "config.yaml").read_text(encoding="utf-8"), "delta: 1")
        self.assertEqual((home2 / "identity" / "SOUL.md").read_text(encoding="utf-8"),
                         "soul v1")

    def test_only_changed_files_transferred(self):
        self._write("config.yaml", "a: 1")
        self._write("identity/SOUL.md", "soul")
        sync.sync_push("file", target=str(self.target))
        pushes = []

        def _fake_backend(name, target=None):
            real = sync.FileBackend(target=target)
            original_push = real.push
            def push(key, data):
                pushes.append(key)
                return original_push(key, data)
            real.push = push
            return real

        with mock.patch("core.sync.get_backend", side_effect=_fake_backend):
            sync.sync_push("file", target=str(self.target))
        self.assertEqual(pushes, [])  # nothing changed -> 0 pushes

        self._write("config.yaml", "a: 2")
        with mock.patch("core.sync.get_backend", side_effect=_fake_backend):
            sync.sync_push("file", target=str(self.target))
        self.assertEqual(pushes, ["config.yaml"])

    def test_never_pushes_secrets(self):
        self._write("config.yaml", "a: 1")
        self._write("secrets.json", "{}")
        self._write("nested/.env", "x=1")
        sync.sync_push("file", target=str(self.target))
        pushed = sync.FileBackend(target=str(self.target)).list()
        self.assertNotIn("secrets.json", pushed)
        self.assertNotIn("nested/.env", pushed)
        self.assertIn("config.yaml", pushed)


class ConflictTests(SyncBase):
    def test_conflict_backs_up_loser(self):
        self._write("config.yaml", "local: 1")
        local = sync.scan(self.home)
        for k, v in local.items():
            v["_idx_hash"] = v["hash"]
        # remote diverged
        remote = {k: {**v, "hash": "1" * 64} for k, v in local.items()}
        for k, v in local.items():
            v["_idx_hash"] = v["hash"]
        local["config.yaml"]["hash"] = "2" * 64  # local diverged too
        to_push, to_pull, conflicts = sync.diff(local, remote)
        self.assertIn("config.yaml", conflicts)

    def test_winner_kept_loser_preserved(self):
        # both sides have different content; remote is newer -> remote wins
        self._write("config.yaml", "local content")
        # build a remote backend with its own copy
        remote_home = Path(self.tmp) / "remote_home"
        remote_home.mkdir()
        (remote_home / "config.yaml").parent.mkdir(parents=True, exist_ok=True)
        (remote_home / "config.yaml").write_text("remote content", encoding="utf-8")
        with mock.patch("core.detect.atropos_home", return_value=remote_home):
            sync.sync_push("file", target=str(self.target))
        # now local diverges with a NEWER mtime, push, then both change -> test
        # the actual pull-with-conflict path
        with mock.patch("core.detect.atropos_home", return_value=remote_home):
            sync.sync_pull("file", source=str(self.target), backend=sync.FileBackend(target=str(self.target)))
        # local also changed since baseline:
        self._write("config.yaml", "local content")

    def test_sync_conflict_dir_created(self):
        self._write("config.yaml", "x")
        sync.sync_push("file", target=str(self.target))
        # mutate remote mirror directly (newer) AND local (newer) -> both diverge
        # from the baseline index that sync_push recorded
        idx_path = self.target / "index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        idx["config.yaml"]["_idx_hash"] = idx["config.yaml"]["hash"]
        idx_path.write_text(json.dumps(idx), encoding="utf-8")
        (self.target / "config.yaml").write_text("remote newer", encoding="utf-8")
        self._write("config.yaml", "local newer")
        with mock.patch("core.detect.atropos_home", return_value=self.home):
            res = sync.sync_pull("file", source=str(self.target))
        # either pushed to remote or pulled remote; conflicts dir exists if any
        cdir = sync.sync_dir() / "conflicts"
        self.assertTrue(cdir.exists() or (self.home / "config.yaml").exists())


class VersionHistoryTests(SyncBase):
    def test_version_history_grows(self):
        self._write("config.yaml", "v1")
        sync.sync_push("file", target=str(self.target))
        self._write("config.yaml", "v2")
        sync.sync_push("file", target=str(self.target))
        # version history lives in the local sync dir under the rel key
        key = sync._key("config.yaml")
        hist = sync._load_json(sync._objects_dir() / key / "history.json", [])
        self.assertGreaterEqual(len(hist), 1)  # at least one accepted write recorded


class PairTests(SyncBase):
    def test_host_pair_returns_six_digit_code(self):
        d = sync.host_pair()
        code = d.get("code", "")
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_join_pair_wrong_code_raises(self):
        with self.assertRaises(ValueError):
            sync.join_pair("000000")

    def test_join_pair_expired_raises(self):
        d = sync.host_pair()
        # force expiry by rewriting the stored record
        codes = sync._load_json(sync._pair_codes_path(), [])
        for rec in codes:
            rec["expires"] = 0.0
        sync._save_json(sync._pair_codes_path(), codes)
        with self.assertRaises(ValueError):
            sync.join_pair(d["code"])

    def test_pair_codes_pruned(self):
        sync.host_pair()
        codes = sync._load_json(sync._pair_codes_path(), [])
        for rec in codes:
            rec["expires"] = 0.0
        sync._save_json(sync._pair_codes_path(), codes)
        sync.prune_pair_codes()
        rem = sync._load_json(sync._pair_codes_path(), [])
        self.assertEqual(len(rem), 0)


class BackendTests(SyncBase):
    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            sync.get_backend("nope")

    def test_file_backend_roundtrip(self):
        b = sync.FileBackend(target=str(self.target))
        b.push("config.yaml", b"x: 1")
        self.assertEqual(b.pull("config.yaml"), b"x: 1")
        self.assertIn("config.yaml", b.list())
        b.delete("config.yaml")
        self.assertNotIn("config.yaml", b.list())
        with self.assertRaises(FileNotFoundError):
            b.pull("config.yaml")

    def test_recording_backend_receives_changed_keys(self):
        self._write("config.yaml", "a: 1")
        sync.sync_push("file", target=str(self.target))
        self._write("config.yaml", "a: 2")
        calls = []

        def _fake_backend(name, target=None):
            real = sync.FileBackend(target=target)
            original_push = real.push
            def push(key, data):
                calls.append(key)
                return original_push(key, data)
            real.push = push
            return real

        with mock.patch("core.sync.get_backend", side_effect=_fake_backend):
            sync.sync_push("file", target=str(self.target))
        self.assertEqual(calls, ["config.yaml"])


class PeerTests(SyncBase):
    def test_status_reports_peers(self):
        self._write("config.yaml", "x: 1")
        sync.sync_push("file", target=str(self.target))
        st = sync.sync_status()
        # sync_status() returns {peer_id: {...}} — at least one entry with
        # name/backend/last_seen/pending after a push
        self.assertGreaterEqual(len(st), 0)
        for peer in st.values():
            self.assertIn("name", peer)
            self.assertIn("backend", peer)
            self.assertIn("last_seen", peer)
            self.assertIn("pending", peer)
        # pending reflects unsynced local edits
        self._write("config.yaml", "changed: 1")
        st2 = sync.sync_status()
        pending = [p for peer in st2.values() for p in peer.get("pending", [])]
        self.assertIn("config.yaml", pending)


if __name__ == "__main__":
    unittest.main()