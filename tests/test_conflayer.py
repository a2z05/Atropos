#!/usr/bin/env python3
"""Atropos conflayer tests — config mirror store, validation, hash-guard
projection, conflicts, snapshots, modes.

Run from the repo root:
    python3 -m unittest tests/test_conflayer.py -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import conflayer, config, detect  # noqa: E402


class ConflayerBase(unittest.TestCase):
    """Isolate every home: ATROPOS_HOME, HERMES_HOME and the OS home dir.

    The Claude-home live paths (~/.claude/settings.json, mcp.json) are
    redirected into the temp dir by temporarily replacing ``detect._home``
    (a private module helper — the only seam available), so no test ever
    touches the real ~/.claude.
    """

    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = Path(tempfile.mkdtemp(prefix="atropos_conflayer_"))
        os.environ["ATROPOS_HOME"] = str(self.tmp)
        self.hermes = self.tmp / ".hermes"
        os.environ["HERMES_HOME"] = str(self.hermes)
        self._orig_home_fn = detect._home
        detect._home = staticmethod(lambda: self.tmp)
        self.claude = self.tmp / ".claude"

    def tearDown(self):
        detect._home = self._orig_home_fn
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def _set_shared(self, name):
        conflayer.mode(name, "shared")

    def _target_live(self, name) -> Path:
        return conflayer.live_path(name)


class ListingTests(ConflayerBase):
    def test_list_has_all_six_configs(self):
        items = conflayer.list_configs()
        self.assertEqual(len(items), 6)
        names = {c["name"] for c in items}
        self.assertEqual(names, set(conflayer.CONFIG_NAMES))
        entry = next(c for c in items if c["name"] == "hermes.yaml")
        self.assertEqual(Path(entry["live_path"]), self.hermes / "config.yaml")
        self.assertEqual(entry["exists"], False)
        self.assertEqual(entry["mode"], "separate")

    def test_mode_defaults_from_settings(self):
        items = conflayer.list_configs()
        self.assertTrue(all(c["mode"] == "separate" for c in items))

    def test_show_returns_canonical(self):
        conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        s = conflayer.show("hermes.yaml")
        self.assertTrue(s["ok"])
        self.assertEqual(s["content"], "telegram:\n  token: x\n")
        self.assertTrue(s["exists"])
        self.assertEqual(Path(s["path"]), conflayer.canonical_path("hermes.yaml"))

    def test_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            conflayer.show("../evil")
        with self.assertRaises(ValueError):
            conflayer.show("nope.ini")

    def test_mode_validation_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            conflayer.mode("hermes.yaml", "mirror")
        # valid modes go through
        for m in conflayer.MODES:
            r = conflayer.mode("hermes.yaml", m)
            self.assertEqual(r["mode"], m)


class ValidationTests(ConflayerBase):
    def test_validate_rejects_bad_json_with_line_col(self):
        r = conflayer.validate("claude.settings.json", '{\n  "permissions": [1,\n}\n')
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"][0]["line"], 3)
        self.assertGreaterEqual(r["errors"][0]["col"], 1)

    def test_validate_accepts_good_json(self):
        r = conflayer.validate("claude.settings.json", '{"permissions": {"allow": ["x"]}}')
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"]["type"], "json")
        self.assertEqual(r["errors"], [])

    def test_validate_accepts_good_yaml(self):
        r = conflayer.validate("hermes.yaml", "telegram:\n  token: x\n")
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"]["type"], "yaml")
        self.assertEqual(r["summary"]["keys"], 1)

    def test_validate_env_parsing(self):
        ok = conflayer.validate("hermes.env", "TOKEN=abc\nCHAT_ID=-100\n# comment\n")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["summary"]["keys"], 2)
        bad = conflayer.validate("hermes.env", "TOKEN=abc\nnot-a-key\nFOO=bar\n")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["errors"][0]["line"], 2)
        self.assertEqual(bad["summary"]["keys"], 2)

    def test_save_rejects_invalid_content(self):
        with self.assertRaises(ValueError) as ctx:
            conflayer.save("claude.settings.json", '{"broken": ')
        self.assertIn("line 1", str(ctx.exception))
        # nothing written
        r = conflayer.show("claude.settings.json")
        self.assertFalse(r["exists"])

    def test_save_accepts_valid_content(self):
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertTrue(r["ok"])
        self.assertEqual(conflayer.show("hermes.yaml")["content"],
                         "telegram:\n  token: x\n")


class ProjectionTests(ConflayerBase):
    def test_shared_save_projects_to_live(self):
        self._set_shared("hermes.yaml")
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["written"]), 1)
        self.assertEqual(r["conflicts"], [])
        self.assertEqual((self.hermes / "config.yaml").read_text(encoding="utf-8"),
                         "telegram:\n  token: x\n")

    def test_separate_default_never_projects(self):
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertEqual(r["written"], [])
        self.assertEqual(r["conflicts"], [])
        self.assertFalse((self.hermes / "config.yaml").exists())

    def test_drift_conflict_and_target_unchanged(self):
        self._set_shared("hermes.yaml")
        conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        # the harness edits its live file on its own
        (self.hermes / "config.yaml").write_text("telegram:\n  token: hacked\n",
                                                 encoding="utf-8")
        r = conflayer.save("hermes.yaml", "telegram:\n  token: new\n")
        self.assertEqual(r["written"], [])
        self.assertEqual(len(r["conflicts"]), 1)
        c = r["conflicts"][0]
        self.assertTrue(c["conflict"])
        self.assertEqual(Path(c["target"]), self.hermes / "config.yaml")
        self.assertNotIn("ok", c)
        self.assertEqual((self.hermes / "config.yaml").read_text(encoding="utf-8"),
                         "telegram:\n  token: hacked\n")

    def test_no_conflict_on_identical_write(self):
        self._set_shared("hermes.yaml")
        conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertEqual(r["conflicts"], [])

    def test_sync_projects_shared(self):
        self._set_shared("hermes.yaml")
        conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        (self.hermes / "config.yaml").write_text("drift", encoding="utf-8")
        r = conflayer.sync("hermes.yaml")
        self.assertEqual(len(r["conflicts"]), 1)
        conflayer.resolve_conflict("hermes.yaml", str(self.hermes / "config.yaml"), "keep")
        r2 = conflayer.sync("hermes.yaml")
        self.assertEqual(r2["conflicts"], [])
        self.assertEqual(len(r2["written"]), 1)

    def test_sync_skips_non_shared(self):
        conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        r = conflayer.sync("hermes.yaml")
        self.assertEqual(r["note"], "mode is separate — nothing to project")

    def test_sync_missing_canonical_raises(self):
        with self.assertRaises(FileNotFoundError):
            conflayer.sync("hermes.yaml")


class ResolveTests(ConflayerBase):
    def setUp(self):
        super().setUp()
        self._set_shared("hermes.yaml")
        conflayer.save("hermes.yaml", "telegram:\n  token: x\n")

    def test_resolve_overwrite_writes_canonical(self):
        (self.hermes / "config.yaml").write_text("drift", encoding="utf-8")
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertEqual(len(r["conflicts"]), 1)
        res = conflayer.resolve_conflict("hermes.yaml",
                                         str(self.hermes / "config.yaml"), "overwrite")
        self.assertEqual(res["action"], "overwrite")
        self.assertEqual((self.hermes / "config.yaml").read_text(encoding="utf-8"),
                         "telegram:\n  token: x\n")
        r2 = conflayer.save("hermes.yaml", "telegram:\n  token: y\n")
        self.assertEqual(r2["conflicts"], [])

    def test_resolve_keep_adopts_local(self):
        (self.hermes / "config.yaml").write_text("local edit", encoding="utf-8")
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertEqual(len(r["conflicts"]), 1)
        res = conflayer.resolve_conflict("hermes.yaml",
                                         str(self.hermes / "config.yaml"), "keep")
        self.assertEqual(res["action"], "keep")
        self.assertEqual((self.hermes / "config.yaml").read_text(encoding="utf-8"),
                         "local edit")
        r2 = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertEqual(r2["conflicts"], [])

    def test_resolve_diff_only_reports(self):
        (self.hermes / "config.yaml").write_text("drift", encoding="utf-8")
        r = conflayer.save("hermes.yaml", "telegram:\n  token: x\n")
        self.assertEqual(len(r["conflicts"]), 1)
        res = conflayer.resolve_conflict("hermes.yaml",
                                         str(self.hermes / "config.yaml"), "diff")
        self.assertTrue(res["differs"])
        self.assertIn("line", res["preview"])
        self.assertEqual((self.hermes / "config.yaml").read_text(encoding="utf-8"),
                         "drift")

    def test_resolve_bad_action_rejected(self):
        with self.assertRaises(ValueError):
            conflayer.resolve_conflict("hermes.yaml",
                                       str(self.hermes / "config.yaml"), "nuke")

    def test_resolve_wrong_target_rejected(self):
        with self.assertRaises(ValueError):
            conflayer.resolve_conflict("hermes.yaml", str(self.tmp / "elsewhere"), "keep")


class RouterKeyedTests(ConflayerBase):
    def test_router_yaml_is_keyed_into_atropos_config(self):
        self._set_shared("router.yaml")
        r = conflayer.save("router.yaml", "active: nain\nmodel: deepmo\n")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["written"]), 1)
        cfg = config.parse_yaml(config.config_path().read_text(encoding="utf-8"))
        # the router section lands merged with the router defaults (the
        # Atropos config always carries the full router mapping)
        self.assertEqual(cfg["router"]["active"], "nain")
        self.assertEqual(cfg["router"]["model"], "deepmo")
        self.assertEqual(cfg["router"]["base_url"], "")
        self.assertEqual(cfg["router"]["api_key_env"], "OPENAI_API_KEY")

    def test_router_unrelated_key_edit_not_conflict(self):
        self._set_shared("router.yaml")
        conflayer.save("router.yaml", "active: nain\n")
        # harness edits a *different* part of config.yaml, preserving the
        # router section exactly as Atropos last wrote it
        p = config.config_path()
        text = p.read_text(encoding="utf-8").rstrip() + "\ndashboard:\n  port: 9000\n"
        p.write_bytes(text.encode("utf-8"))
        r = conflayer.save("router.yaml", "active: nain\n")
        self.assertEqual(r["conflicts"], [])
        self.assertEqual(len(r["written"]), 1)
        # the dashboard edit is still intact after projected save
        cfg = config.parse_yaml(p.read_text(encoding="utf-8"))
        self.assertEqual(cfg["dashboard"]["port"], 9000)

    def test_router_router_section_edit_is_conflict(self):
        self._set_shared("router.yaml")
        conflayer.save("router.yaml", "active: nain\n")
        cfg = config.load()
        cfg["router"] = {"active": "omni"}
        config.save(cfg)
        r = conflayer.save("router.yaml", "active: nain\n")
        self.assertEqual(len(r["conflicts"]), 1)


class HistoryTests(ConflayerBase):
    def test_snapshot_on_every_save(self):
        conflayer.save("hermes.yaml", "a: 1\n")
        conflayer.save("hermes.yaml", "a: 2\n")
        conflayer.save("hermes.yaml", "a: 3\n")
        snaps = conflayer._snapshots("hermes.yaml")
        self.assertEqual(len(snaps), 3)
        self.assertEqual(snaps[0].read_text(encoding="utf-8"), "a: 2\n")

    def test_prune_keeps_last_8(self):
        for i in range(12):
            conflayer.save("hermes.yaml", f"a: {i}\n")
        snaps = conflayer._snapshots("hermes.yaml")
        self.assertEqual(len(snaps), 8)
        self.assertEqual(snaps[0].read_text(encoding="utf-8"), "a: 10\n")
        self.assertEqual(snaps[-1].read_text(encoding="utf-8"), "a: 3\n")

    def test_rollback(self):
        conflayer.save("hermes.yaml", "a: 1\n")
        conflayer.save("hermes.yaml", "a: 2\n")
        r = conflayer.rollback("hermes.yaml", 1)
        self.assertTrue(r["ok"])
        self.assertEqual(conflayer.show("hermes.yaml")["content"], "a: 1\n")

    def test_rollback_out_of_range(self):
        conflayer.save("hermes.yaml", "a: 1\n")
        with self.assertRaises(ValueError):
            conflayer.rollback("hermes.yaml", 9)

    def test_rollback_no_snapshots(self):
        with self.assertRaises(FileNotFoundError):
            conflayer.rollback("hermes.env", 1)


class DiffAndStatsTests(ConflayerBase):
    def test_diff_detects_missing_live(self):
        conflayer.save("hermes.yaml", "a: 1\n")
        d = conflayer.diff("hermes.yaml")
        self.assertTrue(d["differs"])
        self.assertEqual(d["preview"], "(live file missing)")

    def test_diff_detects_drift(self):
        self._set_shared("hermes.yaml")
        conflayer.save("hermes.yaml", "a: 1\n")
        d = conflayer.diff("hermes.yaml")
        self.assertFalse(d["differs"])
        (self.hermes / "config.yaml").write_text("a: 2\n", encoding="utf-8")
        d2 = conflayer.diff("hermes.yaml")
        self.assertTrue(d2["differs"])
        self.assertIn("line", d2["preview"])

    def test_stats(self):
        conflayer.save("hermes.yaml", "a: 1\n")
        conflayer.save("hermes.env", "TOKEN=x\n")
        conflayer.mode("hermes.env", "shared")
        s = conflayer.stats()
        self.assertEqual(s["configs"], 6)
        self.assertGreater(s["total_bytes"], 0)
        self.assertEqual(s["by_mode"].get("separate", 0), 5)
        self.assertEqual(s["by_mode"].get("shared", 0), 1)


if __name__ == "__main__":
    unittest.main()