#!/usr/bin/env python3
"""Atropos AI Update Engine tests — offline, fast, hermetic.

Follows tests/test_lan.py's ATROPOS_HOME isolation pattern. The test runner
(`_run_tests`) and the hack apply machinery are mocked so no real test suite
or git operation ever runs.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import settings, update_ai  # noqa: E402

TEST_HACK = {
    "id": "00-test-rewrite",
    "fn": lambda s: s,  # code registry — the customization IS code
    "target": "plugins/A.py",
    "verify": ["def is_user_authorized(self, uid):"],
    "apply_after": None,
}

OLD_SOURCE = """\
class Builder:
    def is_user_authorized(self, uid):
        return True
"""

NEW_SOURCE = """\
class Builder:
    def is_guest_authorized(self, uid):
        return True
"""


class UpdateAiBase(unittest.TestCase):
    def setUp(self):
        from core import patches
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_ai_")
        os.environ["ATROPOS_HOME"] = self.tmp
        # register the test hack in the in-memory code registry (no YAML files)
        self._orig_patches = patches.HACKS
        patches.HACKS = patches.HACKS + [TEST_HACK]

    def tearDown(self):
        from core import patches
        patches.HACKS = self._orig_patches
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)

    def _attempt(self, rewritten="new: |-\n  def is_guest_authorized(self, uid):\n"):
        return update_ai.append_attempt({
            "patch_id": "00-test-rewrite",
            "upstream_version": "2.0.0",
            "current_version": "1.0.0",
            "status": "suggested",
            "mode": "auto",
            "model": "deepmo",
            "effort": "medium",
            "rewritten_patch": rewritten,
        })


class DiagnosisTests(UpdateAiBase):
    def test_rename_detected(self):
        d = update_ai.diagnose_failure("00-test-rewrite", OLD_SOURCE, NEW_SOURCE)
        self.assertIn("api_renamed", d["reason_categories"])
        self.assertEqual(d["old_token"], "is_user_authorized")
        self.assertEqual(d["rename_candidate"], "is_guest_authorized")
        self.assertIn("is_guest_authorized", d["suggested_action"])

    def test_rename_detected_without_hack_file(self):
        """Differential anchors work even when the descriptor is absent
        (the customization is code; diagnosis must not depend on YAML)."""
        from core import patches
        patches.HACKS = [h for h in patches.HACKS
                          if (h["id"] if isinstance(h, dict) else h[0]) != "00-test-rewrite"]
        d = update_ai.diagnose_failure("00-test-rewrite", OLD_SOURCE, NEW_SOURCE)
        self.assertIn("api_renamed", d["reason_categories"])
        self.assertEqual(d["old_token"], "is_user_authorized")
        self.assertEqual(d["rename_candidate"], "is_guest_authorized")

    def test_conflict_when_no_rename(self):
        gone = "class Builder:\n    def vanished(self):\n        pass\n"
        d = update_ai.diagnose_failure("00-test-rewrite", OLD_SOURCE, gone)
        self.assertIn("conflict", d["reason_categories"])
        self.assertNotIn("api_renamed", d["reason_categories"])

    def test_unknown_with_no_fingerprint(self):
        d = update_ai.diagnose_failure("00-test-rewrite", NEW_SOURCE, NEW_SOURCE)
        self.assertIn("unknown", d["reason_categories"])


class RewriteTests(UpdateAiBase):
    def test_fallback_engine_rewrites_symbol(self):
        d = update_ai.diagnose_failure("00-test-rewrite", OLD_SOURCE, NEW_SOURCE)
        r = update_ai.rewrite_patch("00-test-rewrite", OLD_SOURCE, NEW_SOURCE, d)
        self.assertTrue(r["ok"])
        self.assertEqual(r["engine"], "fallback")
        self.assertIn("is_guest_authorized", r["rewritten_patch"])
        self.assertNotIn("is_user_authorized", r["rewritten_patch"])

    def test_llm_engine_when_provider_configured(self):
        from core import update_ai as ua
        seen = {}

        def fake_provider(patch_text, context):
            seen["ctx"] = context
            return "is_guest_authorized"

        d = ua.diagnose_failure("00-test-rewrite", OLD_SOURCE, NEW_SOURCE)
        with mock.patch.object(ua, "_LLM_PROVIDER", fake_provider):
            r = ua.rewrite_patch("00-test-rewrite", OLD_SOURCE, NEW_SOURCE, d)
        self.assertTrue(r["ok"])
        self.assertEqual(r["engine"], "llm")
        self.assertIn("is_guest_authorized", r["rewritten_patch"])
        self.assertEqual(seen["ctx"]["effort"], "medium")

    def test_llm_rewrite_hook_defaults_to_error(self):
        from core import update_ai as ua
        with mock.patch.object(ua, "_LLM_PROVIDER", None):
            with self.assertRaises(RuntimeError):
                ua.llm_rewrite("x", {})


class ModeTests(UpdateAiBase):
    def test_mode_off_rewrite(self):
        with mock.patch("core.update_ai._settings_mode", return_value="off"):
            r = update_ai.rewrite_patch("00-test-rewrite", OLD_SOURCE, NEW_SOURCE,
                                        {"reason_categories": ["conflict"]})
            self.assertFalse(r["ok"])
            self.assertEqual(r["reason"], "mode off")
            self.assertFalse(update_ai.mode_gate("analyze"))
            self.assertFalse(update_ai.mode_gate("preview"))
            self.assertFalse(update_ai.mode_gate("apply"))

    def test_mode_manual_gate(self):
        with mock.patch("core.update_ai._settings_mode", return_value="manual"):
            self.assertTrue(update_ai.mode_gate("analyze"))
            self.assertTrue(update_ai.mode_gate("preview"))
            self.assertFalse(update_ai.mode_gate("apply"))

    def test_mode_auto_gate(self):
        with mock.patch("core.update_ai._settings_mode", return_value="auto"):
            self.assertTrue(update_ai.mode_gate("analyze"))
            self.assertTrue(update_ai.mode_gate("apply"))


class ApplyTests(UpdateAiBase):
    def _mocks(self, post_tests_fail=False):
        """returns (run_patch, apply_patch, write_patch) as unstarted patches.

        ``_run_tests`` sequence: baseline pass (and post-rewrite fail when
        ``post_tests_fail``); ``patches.apply_hacks`` claims the target patch
        applies; ``_write_patch`` is a no-op.
        """
        results = [update_ai._fake_result(False)]
        if post_tests_fail:
            results.append(update_ai._fake_result(True))
        it = iter(results)
        run = mock.patch("core.update_ai._run_tests",
                         side_effect=lambda *a, **k: next(it, results[-1]))
        patches_apply = mock.patch("core.update_ai.patches.apply_hacks",
                                   return_value=(["00-test-rewrite"], [], []))
        write = mock.patch("core.update_ai._write_patch")
        return run, patches_apply, write

    def test_not_confirmed(self):
        aid = self._attempt()["id"]
        run, pa, wp = self._mocks()
        with run as rt, pa, wp:
            r = update_ai.apply_ai(aid, confirm=False)
        rt.assert_called_once()
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not confirmed")
        self.assertEqual(r["attempt_id"], aid)

    def test_apply_confirms_and_rolls_back_on_test_failure(self):
        aid = self._attempt()["id"]
        run, pa, wp = self._mocks(post_tests_fail=True)
        with run, pa, wp:
            r = update_ai.apply_ai(aid, confirm=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "rolled_back")
        rec = next(a for a in update_ai.load_history()["attempts"] if a["id"] == aid)
        self.assertEqual(rec["status"], "rolled_back")

    def test_apply_success(self):
        aid = self._attempt()["id"]
        run, pa, wp = self._mocks()
        with run, pa, wp, \
             mock.patch("core.update_ai.doctor_checks",
                        return_value=[{"name": "patches", "ok": True, "msg": "ok",
                                       "fixed": False}]) as dc:
            r = update_ai.apply_ai(aid, confirm=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "applied")
        rec = next(a for a in update_ai.load_history()["attempts"] if a["id"] == aid)
        self.assertEqual(rec["status"], "applied")
        dc.assert_called_once()

    def test_mode_off_apply_blocks(self):
        aid = self._attempt()["id"]
        with mock.patch("core.update_ai._settings_mode", return_value="off"):
            r = update_ai.apply_ai(aid, confirm=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "mode off")

    def test_baseline_test_failure_refuses(self):
        aid = self._attempt()["id"]
        with mock.patch("core.update_ai._run_tests",
                        return_value=update_ai._fake_result(True)):
            r = update_ai.apply_ai(aid, confirm=True)
        self.assertFalse(r["ok"])
        self.assertIn("baseline", r["reason"])

    def test_applied_already(self):
        aid = self._attempt()["id"]
        update_ai.update_attempt(aid, status="applied")
        run, pa, wp = self._mocks()
        with run, pa, wp:
            r = update_ai.apply_ai(aid, confirm=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("already"))
        self.assertEqual(r["status"], "applied")

    def test_patch_not_in_pristine_reset_aborts(self):
        """If the rewritten patch fails the pristine pre-check, rollback."""
        aid = self._attempt()["id"]
        run, _, wp = self._mocks()
        apply_broken = mock.patch(
            "core.update_ai.patches.apply_hacks",
            return_value=(["other-patch"], [], []))
        with run, apply_broken, wp:
            r = update_ai.apply_ai(aid, confirm=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], "rolled_back")


class HistoryTests(UpdateAiBase):
    def test_append_id_unique_and_persisted(self):
        a1 = update_ai.append_attempt({"patch_id": "p1", "upstream_version": "2",
                                       "current_version": "1"})
        a2 = update_ai.append_attempt({"patch_id": "p1", "upstream_version": "2",
                                       "current_version": "1"})
        self.assertNotEqual(a1["id"], a2["id"])
        data = json.loads(Path(self.tmp, "update_ai.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["attempts"]), 2)
        self.assertTrue(a1["id"] and a2["id"])

    def test_corrupt_history_degrades(self):
        Path(self.tmp, "update_ai.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(update_ai.load_history(), {"attempts": []})
        a = update_ai.append_attempt({"patch_id": "p1"})
        self.assertTrue(a["id"])

    def test_update_attempt_status(self):
        a = update_ai.append_attempt({"patch_id": "p1", "status": "running"})
        upd = update_ai.update_attempt(a["id"], status="applied", error="x")
        self.assertEqual(upd["status"], "applied")
        self.assertEqual(update_ai.load_history()["attempts"][0]["status"], "applied")
        self.assertIsNone(update_ai.update_attempt("nope", status="applied"))

    def test_append_id_collision_resistance(self):
        """Same patch + same versions still yields distinct ids (ts in id)."""
        a1 = update_ai.append_attempt({"patch_id": "p1", "upstream_version": "2",
                                       "current_version": "1"})
        a2 = update_ai.append_attempt({"patch_id": "p1", "upstream_version": "2",
                                       "current_version": "1"})
        self.assertNotEqual(a1["id"], a2["id"])


class AiCheckTests(UpdateAiBase):
    def test_ai_check_preview_no_apply(self):
        with mock.patch("core.update_ai._settings_mode", return_value="manual"):
            prev = update_ai.ai_check({
                "upstream_version": "2.0.0",
                "current_version": "1.0.0",
                "upstream": NEW_SOURCE,
                "current": OLD_SOURCE,
                "failed_patches": [{"patch_id": "00-test-rewrite",
                                    "current_source": OLD_SOURCE,
                                    "upstream_source": NEW_SOURCE}],
            })
        self.assertTrue(prev["ok"])
        self.assertEqual(len(prev["previews"]), 1)
        p = prev["previews"][0]
        self.assertEqual(p["attempt"]["status"], "suggested")
        self.assertIn("api_renamed", p["diagnosis"]["reason_categories"])
        self.assertIn("is_guest_authorized", p["rewritten_patch"])
        self.assertIn("is_guest_authorized", p["diff"])

    def test_ai_check_mode_off(self):
        with mock.patch("core.update_ai._settings_mode", return_value="off"):
            prev = update_ai.ai_check({"failed_patches": []})
        self.assertFalse(prev["ok"])
        self.assertEqual(prev["reason"], "mode off")


class RunnerTests(UpdateAiBase):
    def test_repo_root(self):
        self.assertEqual(update_ai._detect_repo(), _REPO)

    def test_run_tests_skips_non_atropos(self):
        fake_repo = Path(self.tmp) / "other"
        fake_repo.mkdir()
        res = update_ai._run_tests(fake_repo)
        self.assertEqual(res.returncode, 0)
        self.assertIn("skipped", res.stderr)

    def test_run_tests_timeout_is_failure(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)
        with mock.patch("core.update_ai.subprocess.run", side_effect=boom):
            res = update_ai._run_tests(_REPO)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("timeout", res.stderr)


if __name__ == "__main__":
    unittest.main()


class AiRepairTests(UpdateAiBase):
    """update._ai_repair — the update-time self-modification hook."""

    def test_repair_off_gate(self):
        from core import update
        r = update._ai_repair("repo", {"errors": []}, timeout=10)
        self.assertFalse(r["ok"])
        self.assertIn("auto_ai off", r["reason"])

    def test_mode_off_gate(self):
        from core import update
        settings.set("update.auto_ai", True)
        settings.set("update-ai.mode", "off")
        r = update._ai_repair("repo", {"errors": []}, timeout=10)
        self.assertFalse(r["ok"])
        self.assertIn("mode off", r["reason"])

    def test_repair_diagnoses_and_returns_attempt(self):
        from core import update
        settings.set("update.auto_ai", True)
        settings.set("update-ai.mode", "manual")
        state = {"errors": ["00-test-rewrite: anchor not found upstream"],
                 "head": "abc1234", "prev_head": "old9999"}
        with mock.patch("core.update.patches.load_hacks",
                        return_value=[TEST_HACK]):
            r = update._ai_repair("repo", state, timeout=10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["patch_id"], "00-test-rewrite")
        self.assertTrue(r["attempt_id"])

    def test_repair_nothing_to_diagnose(self):
        from core import update
        settings.set("update.auto_ai", True)
        settings.set("update-ai.mode", "manual")
        r = update._ai_repair("repo", {"errors": []}, timeout=10)
        self.assertFalse(r["ok"])
        self.assertIn("nothing", r["reason"])


if __name__ == "__main__":
    unittest.main()
