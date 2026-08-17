#!/usr/bin/env python3
"""v18 F tests — auto-improve engine (core/autoskill.py).

Covers the five pieces: usage lifecycle (active→stale→archived, pinned
exempt), auto-skill offer (settings-gated, transcript-derived names,
never writes without confirmation), auto-memory offer, attribution log,
and orchestrate (dependency ordering + merge).
"""
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

from core import autoskill, detect


def _backdate(usage_path, name, iso):
    """Rewrite one skill's last_used to a fixed past instant."""
    data = json.loads(usage_path.read_text(encoding="utf-8"))
    data[name]["last_used"] = iso
    usage_path.write_text(json.dumps(data), encoding="utf-8")


class _Hermetic(unittest.TestCase):
    """Sandbox ATROPOS_HOME per case."""

    _SNIP = ("HERMES_HOME",)

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in self._SNIP}
        for k in self._SNIP:
            os.environ.pop(k, None)
        self._home = tempfile.mkdtemp(prefix="atropos_autoskill_")
        os.environ["ATROPOS_HOME"] = self._home
        # settings cache is per-home; fresh read
        import importlib
        import core.settings as _s
        importlib.reload(_s)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("ATROPOS_HOME", None)
        shutil.rmtree(self._home, ignore_errors=True)

    def _mk_skill(self, name: str, tags: str = "") -> Path:
        d = autoskill.skills_dir_for(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test skill\n"
            f"category: general\ntags: [{tags}]\n---\n\nbody\n",
            encoding="utf-8",
        )
        return d


class LifecycleTests(_Hermetic):
    def test_active_new_skill(self):
        self.assertEqual(autoskill.lifecycle("fresh"), "active")

    def test_stale_after_31_days(self):
        self._mk_skill("old")
        autoskill.record_usage("old", "run")
        _backdate(autoskill._usage_path(), "old", "2026-01-01T00:00:00+00:00")
        self.assertEqual(autoskill.lifecycle("old", now="2026-01-15T00:00:00+00:00"),
                         "active")
        self.assertEqual(autoskill.lifecycle("old", now="2026-02-05T00:00:00+00:00"),
                         "stale")
        self.assertEqual(autoskill.lifecycle("old", now="2026-04-05T00:00:00+00:00"),
                         "archived")

    def test_pinned_exempt_from_lifecycle(self):
        self._mk_skill("keep", tags="pinned")
        autoskill.record_usage("keep", "run")
        # 200 days unused — still pinned, never stale/archived
        self.assertEqual(autoskill.lifecycle("keep", now="2027-01-01T00:00:00+00:00"),
                         "pinned")

    def test_sweep_archives_when_past_due(self):
        self._mk_skill("dusty")
        autoskill.record_usage("dusty", "run")
        moves = autoskill.sweep_lifecycle(now="2027-01-01T00:00:00+00:00")
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["name"], "dusty")
        self.assertFalse(autoskill.skills_dir_for("dusty").exists())
        self.assertTrue((autoskill._archive_dir() / "dusty").exists())

    def test_usage_counts_and_stats_shape(self):
        self._mk_skill("used")
        autoskill.record_usage("used", "view")
        autoskill.record_usage("used", "view")
        autoskill.record_usage("used", "run")
        stats = autoskill.usage_stats()
        self.assertEqual(stats["used"]["view"], 2)
        self.assertEqual(stats["used"]["run"], 1)
        self.assertIn("lifecycle", stats["used"])


class AutoSkillOfferTests(_Hermetic):
    def test_offer_gated_off_by_default(self):
        res = autoskill.auto_skill_offer(["step one", "step two", "step three",
                                          "step four", "step five", "step six"])
        self.assertFalse(res["offered"])
        self.assertEqual(res["reason"], "auto_skill off")

    def test_offer_below_threshold(self):
        import core.settings as _s
        _s.set("skills.auto_skill", True)
        res = autoskill.auto_skill_offer(["x"])
        self.assertFalse(res["offered"])
        self.assertIn("need 5", res["reason"])

    def test_offer_names_then_confirmed_write(self):
        import core.settings as _s
        _s.set("skills.auto_skill", True)
        transcript = ["deploy the dashboard to the staging server", "done", "ok"] * 2
        res = autoskill.auto_skill_offer(transcript)
        self.assertTrue(res["offered"])
        self.assertIn("deploy", res["skill_name"])
        # not written yet
        self.assertFalse((autoskill.skills_dir_for(res["skill_name"])).exists())
        confirmed = autoskill.auto_skill_offer(transcript, confirmed=True)
        self.assertTrue(confirmed["ok"])
        self.assertTrue((autoskill.skills_dir_for(confirmed["skill_name"]) / "SKILL.md").exists())

    def test_save_skill_writes_frontmatter(self):
        res = autoskill.save_auto_skill("my-skill", ["one", "two"])
        self.assertTrue(res["ok"])
        text = (autoskill.skills_dir_for("my-skill") / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: my-skill", text)
        self.assertIn("- one", text)
        self.assertIn("- two", text)

    def test_save_duplicate_refused(self):
        autoskill.save_auto_skill("dup", ["a"])
        res = autoskill.save_auto_skill("dup", ["b"])
        self.assertFalse(res["ok"])
        self.assertIn("already exists", res["error"])


class AutoMemoryOfferTests(_Hermetic):
    def test_gated_off(self):
        res = autoskill.auto_memory_offer("some durable context worth keeping")
        self.assertFalse(res["offered"])
        self.assertEqual(res["reason"], "auto_memory off")

    def test_offer_and_confirm_requires_durable_text(self):
        import core.settings as _s
        _s.set("skills.auto_memory", True)
        short = autoskill.auto_memory_offer("x")
        self.assertFalse(short["offered"])
        offered = autoskill.auto_memory_offer(
            "the deployment flow moved to a two-step confirm in v18")
        self.assertTrue(offered["offered"])
        saved = autoskill.auto_memory_offer(
            "the deployment flow moved to a two-step confirm in v18", confirmed=True)
        self.assertTrue(saved["saved"])


class AttributionTests(_Hermetic):
    def test_record_and_history(self):
        rec = autoskill.record_edit("core/tools.py", actor="user", detail="review")
        self.assertEqual(rec["actor"], "user")
        history = autoskill.attribution_for("core/tools.py")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["detail"], "review")

    def test_history_newest_first_and_scoped(self):
        autoskill.record_edit("a.py")
        autoskill.record_edit("b.py")
        autoskill.record_edit("a.py")
        hist = autoskill.attribution_for("a.py")
        self.assertEqual(len(hist), 2)
        self.assertGreaterEqual(hist[0]["ts"], hist[1]["ts"])
        self.assertEqual(autoskill.attribution_for("nope.py"), [])

    def test_missing_file_returns_empty(self):
        self.assertEqual(autoskill.attribution_for("ghost.py"), [])


class OrchestrateTests(_Hermetic):
    def test_dependency_order_applied(self):
        calls = []

        class FakeAgents:
            @staticmethod
            def run_agent(name, prompt):
                calls.append(name)
                return {"ok": True, "reply": f"{name}:{prompt}"}

        import core.autoskill as ak
        orig = ak  # module already imported; monkeypatch the import target
        # monkeypatch core.agents.run_agent via the module attribute lookup
        import core.agents as real_agents
        stash = real_agents.run_agent
        real_agents.run_agent = FakeAgents.run_agent
        try:
            res = ak.orchestrate(
                "build the thing",
                [
                    {"id": "a", "agent": "alpha", "prompt": "first"},
                    {"id": "b", "agent": "beta", "prompt": "second"},
                    {"id": "c", "agent": "gamma", "prompt": "third"},
                ],
                deps={"b": ["a"], "c": ["a", "b"]},
            )
        finally:
            real_agents.run_agent = stash
        self.assertTrue(res["ok"])
        self.assertEqual(res["order"], ["a", "b", "c"])
        self.assertEqual(calls, ["alpha", "beta", "gamma"])

    def test_dependency_cycle_detected(self):
        import core.autoskill as ak
        res = ak.orchestrate(
            "cycle",
            [{"id": "x", "agent": "one", "prompt": "p"},
             {"id": "y", "agent": "two", "prompt": "p"}],
            deps={"x": ["y"], "y": ["x"]},
        )
        self.assertFalse(res["ok"])
        self.assertIn("cycle", res["error"])

    def test_merge_joins_outputs_and_empty_guard(self):
        import core.autoskill as ak
        parts = ak._merge_results(
            {"a": {"ok": True, "result": "ONE"}, "b": {"ok": False, "result": None}},
            "goal", None,
        )
        self.assertEqual(parts, "ONE")
        empty = ak._merge_results({}, "goal", None)
        self.assertIn("no subtask output", empty)


class CuratorTests(_Hermetic):
    def test_status_reports_duplicates_and_stale(self):
        self._mk_skill("alpha", tags="")
        self._mk_skill("beta", tags="")
        self._mk_skill("dup-a", tags="")
        self._mk_skill("dup-b", tags="")
        # two near-identical descriptions
        for name in ("dup-a", "dup-b"):
            d = autoskill.skills_dir_for(name)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: pack the node backups every night\n---\n\nb\n",
                encoding="utf-8",
            )
        status = autoskill.curator_status()
        self.assertGreaterEqual(status["total"], 4)
        self.assertTrue(any("dup-a" in pair and "dup-b" in pair
                            for pair in status["duplicates"]))

    def test_run_consolidates_duplicates(self):
        self._mk_skill("left", tags="")
        self._mk_skill("right", tags="")
        for name in ("left", "right"):
            d = autoskill.skills_dir_for(name)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: scan the log files for errors\n---\n\nb\n",
                encoding="utf-8",
            )
        report = autoskill.curator_run(consolidate=True)
        self.assertIn("right", report["consolidated"])  # alphabetically later dropped
        self.assertTrue((autoskill._archive_dir() / "right").exists())


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(_REPO))
    unittest.main()