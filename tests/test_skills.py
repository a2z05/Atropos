#!/usr/bin/env python3
"""v18 I tests — skill lint / matching / view machinery in core/skills.py.

Frontmatter validation follows Hermes skill_manager_tool._validate_frontmatter;
platform/environment matching follows agent/skill_utils.py. All hermetic via
ATROPOS_HOME swap.
"""
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

from core import skills

GOOD = """\
---
name: my-skill
description: Does the thing.
category: coding
---

Step 1. Do the thing.
Step 2. Done.
"""


def _write_skill(root: Path, name: str, content: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


class _Hermetic(unittest.TestCase):
    _SNIP = ("HERMES_HOME", "ATROPOS_HOME")

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in self._SNIP}
        for k in self._SNIP:
            os.environ.pop(k, None)
        self._home = Path(tempfile.mkdtemp(prefix="atropos_skills_"))
        os.environ["ATROPOS_HOME"] = str(self._home)
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

    def _root(self) -> Path:
        return self._home / "skills"


class ParseFrontmatterTests(_Hermetic):
    def test_parses_frontmatter_and_body(self):
        meta, body = skills.parse_frontmatter(GOOD)
        self.assertEqual(meta["name"], "my-skill")
        self.assertEqual(meta["description"], "Does the thing.")
        self.assertIn("Step 1", body)

    def test_leading_bom_stripped(self):
        content = "﻿" + GOOD
        meta, _ = skills.parse_frontmatter(content)
        self.assertEqual(meta["name"], "my-skill")

    def test_no_frontmatter_returns_empty(self):
        meta, body = skills.parse_frontmatter("# plain\nbody")
        self.assertEqual(meta, {})
        self.assertIn("# plain", body)

    def test_unclosed_fence_returns_empty(self):
        meta, body = skills.parse_frontmatter("---\nname: x\n# no close")
        self.assertEqual(meta, {})

    def test_quoted_values_stripped(self):
        meta, _ = skills.parse_frontmatter(
            '---\nname: "quoted"\ndescription: \'single\'\n---\n\nbody')
        self.assertEqual(meta["name"], "quoted")
        self.assertEqual(meta["description"], "single")


class LintTests(_Hermetic):
    def test_good_skill_passes(self):
        _write_skill(self._root(), "my-skill", GOOD)
        r = skills.skill_lint(root=self._root())
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["ok"], 1)
        self.assertEqual(r["issues"], [])

    def test_dir_without_skilly_md_is_not_a_skill(self):
        """A dir with no SKILL.md is a category container (Hermes layout),
        not a broken skill — skipped from lint."""
        d = self._root() / "category"
        d.mkdir(parents=True, exist_ok=True)
        (d / "DESCRIPTION.md").write_text("category overivew", encoding="utf-8")
        _write_skill(d, "real-skill", GOOD)
        r = skills.skill_lint(root=self._root())
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["ok"], 1)
        self.assertEqual(r["issues"], [])

    def test_missing_fields_flagged(self):
        _write_skill(self._root(), "bad", "---\ncategory: x\n---\n\nbody")
        r = skills.skill_lint(root=self._root())
        self.assertEqual(len(r["issues"]), 1)
        errs = r["issues"][0]["errors"]
        self.assertIn("name", " ".join(errs))
        self.assertIn("description", " ".join(errs))

    def test_long_description_flagged_only_when_new_skill(self):
        long_desc = "x" * 1200
        _write_skill(self._root(), "longy",
                     f"---\nname: longy\ndescription: {long_desc}\n---\n\nbody")
        r = skills.skill_lint(root=self._root())
        self.assertEqual(len(r["issues"]), 1)
        self.assertIn("1024", " ".join(r["issues"][0]["errors"]))

    def test_unclosed_fence_flagged(self):
        _write_skill(self._root(), "open", "---\nname: open\nno close")
        r = skills.skill_lint(root=self._root())
        self.assertIn("closed", " ".join(r["issues"][0]["errors"]))

    def test_empty_body_flagged(self):
        _write_skill(self._root(), "bodyless", "---\nname: b\ndescription: d\n---\n\n")
        r = skills.skill_lint(root=self._root())
        self.assertIn("content after", " ".join(r["issues"][0]["errors"]))

    def test_excluded_dirs_skipped(self):
        _write_skill(self._root(), ".archive", GOOD)
        _write_skill(self._root(), "real", GOOD)
        r = skills.skill_lint(root=self._root())
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["issues"], [])

    def test_lint_one_named_skill(self):
        _write_skill(self._root(), "good1", GOOD)
        _write_skill(self._root(), "bad1", "no frontmatter here")
        r = skills.skill_lint("bad1", root=self._root())
        self.assertEqual(r["total"], 1)
        self.assertEqual(len(r["issues"]), 1)
        with_errors = skills.skill_lint("good1", root=self._root())
        self.assertEqual(with_errors["ok"], 1)


class MatchTests(_Hermetic):
    def test_no_platforms_matches_everywhere(self):
        self.assertTrue(skills.skill_matches_platform({}))

    def test_windows_tag_matches_on_win32(self):
        with mock.patch.object(skills.sys, "platform", "win32"):
            self.assertTrue(skills.skill_matches_platform({"platforms": ["windows"]}))
            self.assertFalse(skills.skill_matches_platform({"platforms": ["macos"]}))

    def test_multiple_platform_any_match(self):
        with mock.patch.object(skills.sys, "platform", "win32"):
            self.assertTrue(skills.skill_matches_platform(
                {"platforms": ["macos", "linux", "windows"]}))
            self.assertFalse(skills.skill_matches_platform(
                {"platforms": ["macos", "linux"]}))

    def test_string_platform_wrapped(self):
        with mock.patch.object(skills.sys, "platform", "win32"):
            self.assertTrue(skills.skill_matches_platform({"platforms": "windows"}))

    def test_environment_empty_matches(self):
        self.assertTrue(skills.skill_matches_environment({}))

    def test_environment_requires_settings(self):
        import core.settings as _s
        _s.set("skills.environments", ["kanban"])
        self.assertTrue(skills.skill_matches_environment({"environments": ["kanban"]}))
        self.assertFalse(skills.skill_matches_environment({"environments": ["docker"]}))
        self.assertTrue(skills.skill_matches_environment({}))


class SkillViewTests(_Hermetic):
    def test_view_loads_meta_and_body(self):
        _write_skill(self._root(), "my-skill", GOOD)
        v = skills.skill_view("my-skill", root=self._root())
        self.assertTrue(v["ok"])
        self.assertEqual(v["frontmatter"]["name"], "my-skill")
        self.assertIn("Step 1", v["body"])
        self.assertTrue(v["platform_ok"])
        self.assertTrue(v["environment_ok"])
        self.assertTrue(v["matches"])
        self.assertEqual(v["errors"], [])

    def test_view_missing_skill(self):
        v = skills.skill_view("nope", root=self._root())
        self.assertFalse(v["ok"])
        self.assertIn("not found", v["error"])

    def test_view_finds_nested_skill(self):
        """Category/name/ layout (Hermes style) is resolvable by bare name."""
        _write_skill(self._root() / "coding", "my-skill", GOOD)
        v = skills.skill_view("my-skill", root=self._root())
        self.assertTrue(v["ok"])
        self.assertEqual(v["frontmatter"]["name"], "my-skill")

    def test_lint_nested_skills_only(self):
        _write_skill(self._root(), "bad", "no frontmatter")
        _write_skill(self._root() / "coding", "good-nested", GOOD)
        _write_skill(self._root() / "devops", "sick-nested", "---\nname: x\n---\n\nno desc")
        r = skills.skill_lint(root=self._root())
        self.assertEqual(r["total"], 3)
        names = {it["skill"] for it in r["issues"]}
        self.assertEqual(names, {"bad", "sick-nested"})

    def test_view_platform_mismatch_surfaces(self):
        _write_skill(self._root(), "mac-only",
                     "---\nname: mac-only\ndescription: d\nplatforms: [macos]\n---\n\nbody")
        v = skills.skill_view("mac-only", root=self._root())
        self.assertTrue(v["ok"])
        self.assertFalse(v["platform_ok"])


if __name__ == "__main__":
    unittest.main()