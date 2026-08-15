#!/usr/bin/env python3
"""Atropos universal identity tests — canonical store, deployment modes,
projection hash-guard, conflicts, snapshots, import, stats.

Run from the repo root:
    python3 -m unittest tests/test_identity.py -v
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

from core import config, detect, identity  # noqa: E402


class IdentityBase(unittest.TestCase):
    """Isolate every home: ATROPOS_HOME, HERMES_HOME and the OS home dir.

    The Claude-home projection (~/.claude) is redirected into the temp
    dir by temporarily replacing ``detect._home`` (a private module
    helper — the only seam available), so no test ever touches the real
    ~/.claude.
    """

    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = Path(tempfile.mkdtemp(prefix="atropos_identity_"))
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

    def _make_repo_agent(self):
        """Create the repo-root AGENTS.md that DEFAULT_MAP projects to."""
        p = _REPO / "AGENTS.md"
        p.write_text("# Test AGENTS\n", encoding="utf-8")
        self.addCleanup(p.unlink, missing_ok=True)


class RoundtripTests(IdentityBase):
    def test_save_then_read(self):
        identity.save("SOUL.md", "# Soul\nI am Atropos.\n")
        self.assertEqual(identity.read("SOUL.md"), "# Soul\nI am Atropos.\n")
        self.assertEqual(identity.canonical_path("SOUL.md"),
                         detect.atropos_home() / "identity" / "SOUL.md")

    def test_list_entries(self):
        identity.save("SOUL.md", "# Soul\n")
        identity.save("AGENTS.md", "# Agents\n")
        identity.save("prompts/welcome.md", "hi")
        files = identity.list_files()
        names = {f["name"] for f in files}
        self.assertIn("SOUL.md", names)
        self.assertIn("AGENTS.md", names)
        self.assertIn("welcome.md", names)
        soul = next(f for f in files if f["name"] == "SOUL.md")
        self.assertEqual(soul["mode"], "shared")
        self.assertIn("hermes", soul["consumed_by"])
        self.assertEqual(soul["content_excerpt"], "# Soul")
        welcome = next(f for f in files if f["name"] == "welcome.md")
        self.assertEqual(welcome["kind"], "prompt")
        self.assertEqual(welcome["mode"], "atropos-only")
        self.assertEqual(welcome["consumed_by"], [])

    def test_read_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            identity.read("SOUL.md")

    def test_invalid_name_rejected(self):
        with self.assertRaises(ValueError):
            identity.save("../evil.md", "x")

    def test_mode_validation_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            identity.mode("SOUL.md", "nonsense")
        # and the file stays shared
        identity.save("SOUL.md", "x")
        entry = identity._map_entry("SOUL.md")
        self.assertEqual(entry["mode"], "shared")

    def test_mode_updates_and_persists(self):
        identity.mode("SOUL.md", "atropos-only")
        entry = identity._map_entry("SOUL.md")
        self.assertEqual(entry["mode"], "atropos-only")
        # a user mode change makes identity.map a persisted setting
        cfg = config.parse_yaml(
            (detect.atropos_home() / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["identity"]["map"]["SOUL.md"]["mode"], "atropos-only")

    def test_prompt_mode_cannot_be_shared_without_targets(self):
        # prompts have no default targets — shared mode projects nothing
        identity.mode("prompts/welcome.md", "shared")
        self.assertEqual(identity._targets_for("prompts/welcome.md"), {})
        r = identity.save("prompts/welcome.md", "hi")
        self.assertEqual(r["projected"], [])


class ProjectionTests(IdentityBase):
    def _setup_soul(self):
        """Hermes SOUL.md + Claude CLAUDE.md as the pre-existing baseline."""
        self.hermes.mkdir(parents=True, exist_ok=True)
        (self.hermes / "SOUL.md").write_text("hermes baseline", encoding="utf-8")
        self.claude.mkdir(parents=True, exist_ok=True)
        (self.claude / "CLAUDE.md").write_text("claude baseline", encoding="utf-8")

    def test_shared_projection_writes_targets(self):
        self._setup_soul()
        r = identity.save("SOUL.md", "# My Soul\n")
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "shared")
        # the pre-existing harness copies were never written by Atropos
        # and differ from canonical → both are conflicts, not silent
        # overwrites
        self.assertEqual(len(r["conflicts"]), 2)
        self.assertEqual(r["projected"], [])
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"),
                         "hermes baseline")
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"),
                         "claude baseline")
        # acknowledge both targets (keep) and save again → projected
        for t in (self.hermes / "SOUL.md", self.claude / "CLAUDE.md"):
            identity.resolve_conflict("SOUL.md", str(t), "keep")
        r2 = identity.save("SOUL.md", "# My Soul\n")
        self.assertEqual(r2["conflicts"], [])
        self.assertEqual(len(r2["projected"]), 2)
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"), "# My Soul\n")
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"), "# My Soul\n")

    def test_drift_conflict_returns_and_target_unchanged(self):
        self._setup_soul()
        # acknowledge the pre-existing copies (keep) so the first save
        # projects cleanly and establishes the Atropos baseline
        for t in (self.hermes / "SOUL.md", self.claude / "CLAUDE.md"):
            identity.resolve_conflict("SOUL.md", str(t), "keep")
        identity.save("SOUL.md", "# My Soul\n")
        # the harness edits its copy on its own
        (self.hermes / "SOUL.md").write_text("hermes local edit", encoding="utf-8")
        r = identity.save("SOUL.md", "# Updated Soul\n")
        self.assertEqual(len(r["projected"]), 1)
        self.assertEqual(len(r["conflicts"]), 1)
        c = r["conflicts"][0]
        self.assertTrue(c["conflict"])
        self.assertEqual(Path(c["target"]), self.hermes / "SOUL.md")
        self.assertEqual(c["harness"], "hermes")
        self.assertNotIn("ok", c)
        # the drifted target is untouched; the other target still got
        # the latest canonical content
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"),
                         "hermes local edit")
        self.assertEqual((self.claude / "CLAUDE.md").read_text(encoding="utf-8"),
                         "# Updated Soul\n")

    def test_no_duplicate_conflict_within_one_save(self):
        self._setup_soul()
        for t in (self.hermes / "SOUL.md", self.claude / "CLAUDE.md"):
            identity.resolve_conflict("SOUL.md", str(t), "keep")
        identity.save("SOUL.md", "# My Soul\n")
        (self.hermes / "SOUL.md").write_text("drift", encoding="utf-8")
        (self.claude / "CLAUDE.md").write_text("drift", encoding="utf-8")
        r = identity.save("SOUL.md", "# New\n")
        self.assertEqual(len(r["conflicts"]), 2)
        self.assertEqual(r["projected"], [])

    def test_separate_mode_never_projects(self):
        self._setup_soul()
        identity.mode("SOUL.md", "separate")
        r = identity.save("SOUL.md", "# My Soul\n")
        self.assertEqual(r["projected"], [])
        self.assertEqual(r["conflicts"], [])
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"),
                         "hermes baseline")

    def test_atropos_only_never_projects(self):
        self._setup_soul()
        identity.mode("SOUL.md", "atropos-only")
        r = identity.save("SOUL.md", "# My Soul\n")
        self.assertEqual(r["projected"], [])
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"),
                         "hermes baseline")

    def test_sync_projects_with_hash_guard(self):
        self._setup_soul()
        identity.save("SOUL.md", "# My Soul\n")  # two conflicts (pre-existing)
        for t in (self.hermes / "SOUL.md", self.claude / "CLAUDE.md"):
            identity.resolve_conflict("SOUL.md", str(t), "keep")
        identity.sync("SOUL.md")  # establishes the baseline
        (self.hermes / "SOUL.md").write_text("drift", encoding="utf-8")
        r = identity.sync("SOUL.md")
        self.assertEqual(len(r["conflicts"]), 1)
        self.assertEqual(len(r["projected"]), 1)
        # resolve with keep → next sync is clean
        identity.resolve_conflict("SOUL.md", str(self.hermes / "SOUL.md"), "keep")
        r2 = identity.sync("SOUL.md")
        self.assertEqual(r2["conflicts"], [])
        self.assertEqual(len(r2["projected"]), 2)
        # and the target now holds canonical content again
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"),
                         "# My Soul\n")

    def test_sync_skips_non_shared(self):
        identity.mode("SOUL.md", "separate")
        identity.save("SOUL.md", "x")
        r = identity.sync("SOUL.md")
        self.assertEqual(r["note"], "mode is separate — nothing to project")


class ResolveTests(IdentityBase):
    def setUp(self):
        super().setUp()
        self.hermes.mkdir(parents=True, exist_ok=True)
        (self.hermes / "SOUL.md").write_text("baseline", encoding="utf-8")
        # acknowledge the pre-existing copy so the first save establishes
        # a clean Atropos baseline (no conflict noise)
        identity.resolve_conflict("SOUL.md", str(self.hermes / "SOUL.md"), "keep")

    def test_resolve_overwrite_writes_canonical(self):
        identity.save("SOUL.md", "# Canonical\n")
        (self.hermes / "SOUL.md").write_text("drift", encoding="utf-8")
        r = identity.save("SOUL.md", "# Canonical\n")
        self.assertEqual(len(r["conflicts"]), 1)
        res = identity.resolve_conflict("SOUL.md", str(self.hermes / "SOUL.md"), "overwrite")
        self.assertEqual(res["action"], "overwrite")
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"),
                         "# Canonical\n")
        # next save is conflict-free
        r2 = identity.save("SOUL.md", "# Canonical v2\n")
        self.assertEqual(r2["conflicts"], [])

    def test_resolve_keep_adopts_local(self):
        identity.save("SOUL.md", "# Canonical\n")
        (self.hermes / "SOUL.md").write_text("local edit", encoding="utf-8")
        r = identity.save("SOUL.md", "# Canonical\n")
        self.assertEqual(len(r["conflicts"]), 1)
        res = identity.resolve_conflict("SOUL.md", str(self.hermes / "SOUL.md"), "keep")
        self.assertEqual(res["action"], "keep")
        # target untouched
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"), "local edit")
        # and now Atropos acknowledges the local state: no conflict next time
        r2 = identity.save("SOUL.md", "# Canonical\n")
        self.assertEqual(r2["conflicts"], [])

    def test_resolve_diff_only_reports(self):
        identity.save("SOUL.md", "# Canonical\n")
        (self.hermes / "SOUL.md").write_text("drift", encoding="utf-8")
        r = identity.save("SOUL.md", "# Canonical\n")
        self.assertEqual(len(r["conflicts"]), 1)
        res = identity.resolve_conflict("SOUL.md", str(self.hermes / "SOUL.md"), "diff")
        self.assertTrue(res["differs"])
        self.assertIn("line", res["preview"])
        # nothing changed
        self.assertEqual((self.hermes / "SOUL.md").read_text(encoding="utf-8"), "drift")
        # and a fresh save still conflicts
        r2 = identity.save("SOUL.md", "# Canonical\n")
        self.assertEqual(len(r2["conflicts"]), 1)

    def test_resolve_bad_action_rejected(self):
        with self.assertRaises(ValueError):
            identity.resolve_conflict("SOUL.md", str(self.hermes / "SOUL.md"), "nuke")

    def test_resolve_unmapped_target_rejected(self):
        identity.save("SOUL.md", "x")
        with self.assertRaises(ValueError):
            identity.resolve_conflict("SOUL.md", str(self.tmp / "nowhere.md"), "overwrite")


class HistoryTests(IdentityBase):
    def test_snapshot_created_on_every_save(self):
        identity.save("SOUL.md", "v1")
        identity.save("SOUL.md", "v2")
        identity.save("SOUL.md", "v3")
        snaps = identity._snapshots("SOUL.md")
        self.assertEqual(len(snaps), 3)
        # the newest snapshot holds the pre-third-save state (v2); the
        # oldest holds the pre-first-save state (empty store)
        self.assertEqual(snaps[0].read_text(encoding="utf-8"), "v2")
        self.assertEqual(snaps[2].read_text(encoding="utf-8"), "")

    def test_prune_keeps_last_8(self):
        for i in range(12):
            identity.save("SOUL.md", f"v{i}")
        snaps = identity._snapshots("SOUL.md")
        self.assertEqual(len(snaps), 8)
        # newest snapshot = the state just before save #12 → v10;
        # oldest kept snapshot = the state just before save #5 → v3
        self.assertEqual(snaps[0].read_text(encoding="utf-8"), "v10")
        self.assertEqual(snaps[-1].read_text(encoding="utf-8"), "v3")

    def test_restore(self):
        identity.save("SOUL.md", "v1")
        identity.save("SOUL.md", "v2")
        # n=1 restores the newest snapshot (state before the last save)
        r = identity.restore("SOUL.md", 1)
        self.assertTrue(r["ok"])
        self.assertEqual(identity.read("SOUL.md"), "v1")
        # restore is itself snapshotted: the pre-restore state (v2) is
        # now the newest snapshot
        snaps = identity._snapshots("SOUL.md")
        self.assertEqual(len(snaps), 3)
        self.assertEqual(snaps[0].read_text(encoding="utf-8"), "v2")
        # n=2 restores the next-older snapshot: after the first restore the
        # history is [v2 (pre-restore), v1, empty] → n=2 = v1
        r2 = identity.restore("SOUL.md", 2)
        self.assertTrue(r2["ok"])
        self.assertEqual(identity.read("SOUL.md"), "v1")
        # the second restore snapshot the pre-restore state (v1) on top
        self.assertEqual(identity._snapshots("SOUL.md")[0].read_text(encoding="utf-8"),
                         "v1")

    def test_restore_out_of_range(self):
        identity.save("SOUL.md", "v1")
        with self.assertRaises(ValueError):
            identity.restore("SOUL.md", 5)
        with self.assertRaises(ValueError):
            identity.restore("SOUL.md", 0)


class ImportTests(IdentityBase):
    def test_detect_new_finds_harness_files(self):
        self.hermes.mkdir(parents=True, exist_ok=True)
        (self.hermes / "SOUL.md").write_text("hermes soul", encoding="utf-8")
        self.claude.mkdir(parents=True, exist_ok=True)
        (self.claude / "CLAUDE.md").write_text("claude soul", encoding="utf-8")
        found = identity.detect_new()
        entries = {c["file"]: c for c in found}
        self.assertIn(str(self.hermes / "SOUL.md"), entries)
        self.assertEqual(entries[str(self.hermes / "SOUL.md")]["source"], "hermes")
        self.assertIn(str(self.claude / "CLAUDE.md"), entries)
        self.assertEqual(entries[str(self.claude / "CLAUDE.md")]["name"], "SOUL.md")
        self.assertEqual(entries[str(self.claude / "CLAUDE.md")]["source"], "claude")

    def test_detect_new_excludes_registered(self):
        # is the repo-root AGENTS.md present? (this project maintains one)
        repo_agents = _REPO / "AGENTS.md"
        if not repo_agents.exists():
            repo_agents.write_text("# Test AGENTS\n", encoding="utf-8")
            self.addCleanup(repo_agents.unlink, missing_ok=True)
        self.hermes.mkdir(parents=True, exist_ok=True)
        (self.hermes / "SOUL.md").write_text("hermes soul", encoding="utf-8")
        identity.import_file("SOUL.md", self.hermes / "SOUL.md")
        identity.import_file("AGENTS.md", repo_agents)
        found = identity.detect_new()
        self.assertEqual(found, [])

    def test_import_creates_canonical_and_registers(self):
        self.hermes.mkdir(parents=True, exist_ok=True)
        src = self.hermes / "AGENTS.md"
        src.write_text("hermes agents", encoding="utf-8")
        r = identity.import_file("AGENTS.md", src, mode="shared")
        self.assertTrue(r["ok"])
        self.assertEqual(identity.read("AGENTS.md"), "hermes agents")
        entry = identity._map_entry("AGENTS.md")
        self.assertEqual(entry["mode"], "shared")
        self.assertIn("repo", entry["targets"])

    def test_import_duplicate_rejected(self):
        self.hermes.mkdir(parents=True, exist_ok=True)
        src = self.hermes / "AGENTS.md"
        src.write_text("x", encoding="utf-8")
        identity.import_file("AGENTS.md", src)
        with self.assertRaises(ValueError):
            identity.import_file("AGENTS.md", src)

    def test_import_missing_source_raises(self):
        with self.assertRaises(FileNotFoundError):
            identity.import_file("AGENTS.md", self.tmp / "nope.md")
        with self.assertRaises(FileNotFoundError):
            identity.import_file("AGENTS.md", str(self.tmp / "nope.md"))

    def test_import_bad_mode_rejected(self):
        self.hermes.mkdir(parents=True, exist_ok=True)
        src = self.hermes / "AGENTS.md"
        src.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            identity.import_file("AGENTS.md", src, mode="mirror")


class DiffAndStatsTests(IdentityBase):
    def test_diff_reports_drift(self):
        self.hermes.mkdir(parents=True, exist_ok=True)
        (self.hermes / "SOUL.md").write_text("same", encoding="utf-8")
        identity.save("SOUL.md", "same")
        d = identity.diff("SOUL.md")
        self.assertTrue(d["ok"])
        by_target = {x["target"]: x for x in d["diffs"]}
        hermes_entry = by_target[str(self.hermes / "SOUL.md")]
        self.assertFalse(hermes_entry["differs"])
        # drift the target and diff again
        (self.hermes / "SOUL.md").write_text("different", encoding="utf-8")
        d2 = identity.diff("SOUL.md")
        by_target = {x["target"]: x for x in d2["diffs"]}
        self.assertTrue(by_target[str(self.hermes / "SOUL.md")]["differs"])
        self.assertIn("line", by_target[str(self.hermes / "SOUL.md")]["preview"])

    def test_diff_missing_canonical_raises(self):
        with self.assertRaises(FileNotFoundError):
            identity.diff("SOUL.md")

    def test_stats(self):
        identity.save("SOUL.md", "soul content")
        identity.save("CODE_STYLE.md", "style")
        identity.mode("SOUL.md", "atropos-only")
        s = identity.stats()
        self.assertEqual(s["files"], 2)
        self.assertGreater(s["total_bytes"], 0)
        self.assertEqual(s["by_mode"], {"shared": 1, "atropos-only": 1})

    def test_keyed_target_system_prompt(self):
        # SYSTEM.md projects into hermes config.yaml under system_prompt
        self.hermes.mkdir(parents=True, exist_ok=True)
        cfg = self.hermes / "config.yaml"
        cfg.write_text("telegram:\n  token: x\n", encoding="utf-8")
        r = identity.save("SYSTEM.md", "be concise")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["projected"]), 1)
        self.assertIn("system_prompt: be concise",
                      cfg.read_text(encoding="utf-8"))
        # second save (unchanged state) must not conflict
        r2 = identity.save("SYSTEM.md", "be concise")
        self.assertEqual(r2["conflicts"], [])
        # drift in an unrelated key is not a conflict
        cfg.write_text("telegram:\n  token: y\nsystem_prompt: be concise\n",
                       encoding="utf-8")
        r3 = identity.save("SYSTEM.md", "be concise")
        self.assertEqual(r3["conflicts"], [])
        # drift in the key itself is a conflict
        cfg.write_text("system_prompt: someone else\n", encoding="utf-8")
        r4 = identity.save("SYSTEM.md", "be concise")
        self.assertEqual(len(r4["conflicts"]), 1)
        self.assertEqual(Path(r4["conflicts"][0]["target"]), cfg)
        self.assertNotIn("ok", r4["conflicts"][0])
        self.assertEqual(cfg.read_text(encoding="utf-8"),
                         "system_prompt: someone else\n")


if __name__ == "__main__":
    unittest.main()
