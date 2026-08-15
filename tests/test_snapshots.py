#!/usr/bin/env python3
"""Atropos snapshots tests — create/list/restore roundtrip, prune."""

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

from core import config, detect, snapshots  # noqa: E402


class SnapshotsBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_snaps_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")
        self.snap_dir = detect.atropos_home() / "snapshots"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def _write_config(self, text="router:\n  active: nain\n"):
        cfg = config.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text, encoding="utf-8")
        return cfg


class SnapshotCreateTests(SnapshotsBase):
    def test_create_shape(self):
        self._write_config()
        res = snapshots.create("pre-update")
        self.assertTrue(res["ok"])
        self.assertEqual(res["label"], "pre-update")
        self.assertRegex(res["name"], r"^\d{8}_\d{6}-pre-update\.tar\.gz$")
        self.assertTrue((self.snap_dir / res["name"]).exists())
        self.assertGreater(res["size"], 0)

    def test_slug_sanitized(self):
        self._write_config()
        res = snapshots.create("My Snapshot!! v1.2")
        self.assertEqual(res["label"], "My Snapshot!! v1.2")
        # unsupported characters become " - ", dots are preserved (YAML-ish)
        self.assertRegex(res["name"], r"-My-Snapshot-v1\.2\.tar\.gz$")

    def test_tarball_contents(self):
        cfg = self._write_config()
        ident = detect.atropos_home() / "identity"
        ident.mkdir(parents=True, exist_ok=True)
        (ident / "persona.md").write_text("# persona", encoding="utf-8")
        res = snapshots.create("full")
        import tarfile
        with tarfile.open(self.snap_dir / res["name"], "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("config.yaml", names)
        self.assertIn("identity/persona.md", names)
        self.assertIn("settings.yaml", names)

    def test_list_snapshots(self):
        self._write_config()
        snapshots.create("one")
        snapshots.create("two")
        rows = snapshots.list_snapshots()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["label"], "two")  # newest first
        self.assertGreater(rows[0]["size"], 0)
        self.assertTrue(rows[0]["ts"])
        # empty dir -> empty list
        shutil.rmtree(self.snap_dir)
        self.assertEqual(snapshots.list_snapshots(), [])


class SnapshotRestoreTests(SnapshotsBase):
    def test_restore_roundtrip(self):
        self._write_config("router:\n  active: nain\n")
        snapshots.create("before")
        # mutate live config
        config.set_path("router.active", "omni")
        self.assertEqual(config.get("router.active"), "omni")
        name = snapshots.list_snapshots()[0]["name"]
        res = snapshots.restore(name)
        self.assertTrue(res["ok"])
        self.assertIn("config.yaml", res["restored"])
        self.assertEqual(config.get("router.active"), "nain")  # restored

    def test_restore_missing_snapshot(self):
        res = snapshots.restore("nope.tar.gz")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_restore_writes_identity_restore_copy(self):
        self._write_config()
        ident = detect.atropos_home() / "identity"
        ident.mkdir(parents=True, exist_ok=True)
        (ident / "persona.md").write_text("# v1", encoding="utf-8")
        snapshots.create("with-identity")
        (ident / "persona.md").write_text("# v2", encoding="utf-8")
        name = snapshots.list_snapshots()[0]["name"]
        res = snapshots.restore(name)
        self.assertTrue(res["ok"])
        self.assertIn("persona.md", res["restored"])
        self.assertEqual((ident / "persona.md").read_text(encoding="utf-8"), "# v2")  # untouched
        self.assertEqual((ident / "persona.md.restore").read_text(encoding="utf-8"), "# v1")

    def test_restore_staging_cleaned(self):
        self._write_config()
        snapshots.create("clean")
        name = snapshots.list_snapshots()[0]["name"]
        snapshots.restore(name)
        staging = self.snap_dir / ".restore"
        self.assertFalse(staging.exists())


class SnapshotPruneTests(SnapshotsBase):
    def test_prune_keeps_newest(self):
        self._write_config()
        for label in ("a", "b", "c"):
            snapshots.create(label)
        removed = snapshots.prune(keep=2)
        self.assertEqual(len(removed), 1)
        remaining = snapshots.list_snapshots()
        self.assertEqual(len(remaining), 2)
        remaining_names = [s["name"] for s in remaining]
        self.assertNotIn(removed[0], remaining_names)
        # the oldest snapshot (lexicographically smallest ts prefix) was pruned
        self.assertEqual(removed[0], sorted(remaining_names + removed)[0])
        self.assertTrue(all((self.snap_dir / n).exists() for n in remaining_names))
        self.assertFalse((self.snap_dir / removed[0]).exists())

    def test_prune_nothing_when_under_limit(self):
        self._write_config()
        snapshots.create("only")
        self.assertEqual(snapshots.prune(keep=10), [])


if __name__ == "__main__":
    unittest.main()
