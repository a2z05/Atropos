#!/usr/bin/env python3
"""Atropos audit tests — resource matrix discovery, shape, summary counts."""

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

from core import audit  # noqa: E402
from core import detect  # noqa: E402


def _yaml_dump(data: dict) -> str:
    """Tiny YAML subset writer for fake hermes configs."""
    out = []
    for k, v in data.items():
        if isinstance(v, dict):
            out.append(f"{k}:")
            for sk, sv in v.items():
                out.append(f"  {sk}: {sv}")
        elif isinstance(v, list):
            out.append(f"{k}:")
            for item in v:
                out.append(f"  - {item}")
        else:
            out.append(f"{k}: {v}")
    return "\n".join(out) + "\n"


class AuditBase(unittest.TestCase):
    """Fake HERMES_HOME + ~/.claude + ATROPOS_HOME in a temp dir."""

    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_audit_")
        self.home = Path(self.tmp)
        os.environ["HERMES_HOME"] = str(self.home / ".hermes")
        os.environ["ATROPOS_HOME"] = str(self.home / ".atropos")
        # ~/.claude must point inside the temp box so the test is hermetic.
        self._home_patch = mock.patch.object(
            detect, "_home", return_value=self.home)
        self._home_patch.start()
        self.hermes = self.home / ".hermes"
        self.claude = self.home / ".claude"
        self.atropos = self.home / ".atropos"

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def write(self, path: Path, content: str = ""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class DiscoveryTests(AuditBase):
    def test_fake_resources_discovered(self):
        """Every fake resource from the brief is found with the right status."""
        # hermes side
        self.write(self.hermes / "skills" / "foo" / "SKILL.md",
                   "---\ndescription: fake skill\n---\n")
        self.write(self.hermes / "plugins" / "p" / "plugin.yaml", "name: p\n")
        self.write(self.hermes / ".env", "OPENAI_API_KEY=x\n")
        self.write(self.hermes / "hooks" / "h.py", "# hook\n")
        self.write(self.hermes / "cron" / "backup.yaml", "schedule: daily\n")
        self.write(self.hermes / "sessions" / "s1.jsonl", "{}\n")
        self.write(self.hermes / "config.yaml", _yaml_dump({"mcp": {}}))
        self.write(self.hermes / "assets" / "guest_persona.md", "# persona\n")
        self.write(self.hermes / "MEMORY.md", "# memory\n")
        # claude side
        self.write(self.claude / "skills" / "bar" / "SKILL.md",
                   "---\ndescription: fake claude skill\n---\n")
        self.write(self.claude / "settings.json",
                   json.dumps({"default_model": "sonnet"}))
        self.write(self.claude / "commands" / "c.yml", "name: c\n")
        self.write(self.claude / "CLAUDE.md", "# claude memory\n")
        # atropos side — canonical stores
        self.write_json(self.atropos / "mcp_servers.json", [])
        self.write_json(self.atropos / "models.json", {"entries": []})
        self.write_json(self.atropos / "commands.json",
                        {"commands": [], "aliases": {}})
        self.write_json(self.atropos / "webhooks.json", [])
        self.write_json(self.atropos / "fleet.json", [])
        self.write_json(self.atropos / "links.json", [])
        self.write(self.atropos / "config.yaml", "router:\n  active: nain\n")
        self.write(self.atropos / "identity" / "MEMORY.md", "# id\n")
        self.write(self.atropos / "configs" / "hermes.yaml", "x: 1\n")
        self.write(self.atropos / "memory" / "notes.json", "[]")
        self.write(self.atropos / "backups" / "keep.txt", "")
        self.write(self.atropos / "cron_state.json", "{}")
        self.write(self.atropos / "alert_state.json", "{}")
        self.write(self.atropos / "update_state.json", "{}")

        rows = audit.table()
        by_name = {r["resource"]: r for r in rows}
        self.assertEqual(len(rows), len(audit.CATEGORIES))

        # each fake resource found
        for resource in ("mcp", "models", "commands", "env", "secrets",
                         "identity files", "config files", "webhooks",
                         "cron", "skills", "plugins", "hooks", "personas",
                         "routers", "backups", "alerts", "jailbreak",
                         "sessions", "fleet", "marketplace", "memory",
                         "update", "permissions", "templates"):
            self.assertTrue(by_name[resource]["found"],
                            f"{resource} should be found")

        # statuses: canonical for atropos-managed, monitored otherwise
        for resource in ("mcp", "models", "commands", "aliases",
                         "identity files", "config files", "webhooks",
                         "skills", "effort", "routers", "backups", "alerts",
                         "jailbreak", "marketplace", "fleet", "memory",
                         "themes", "i18n", "update", "permissions"):
            self.assertEqual(by_name[resource]["atropos_status"], "canonical",
                             f"{resource} should be canonical")
        for resource in ("env", "secrets", "templates", "cron", "plugins",
                         "hooks", "personas", "sessions", "logs"):
            self.assertEqual(by_name[resource]["atropos_status"], "monitored",
                             f"{resource} should be monitored")
        self.assertEqual(by_name["tui"]["atropos_status"], "ignored")

        # recommendations present on every row
        for r in rows:
            self.assertIn(r["recommendation"],
                          ("manage-in-atropos", "monitor", "leave"))

    def test_missing_resources_not_found(self):
        """Empty box: nothing is found, but the matrix is still complete."""
        rows = audit.table()
        by_name = {r["resource"]: r for r in rows}
        for resource in ("skills", "plugins", "hooks", "cron", "env",
                         "sessions", "mcp", "models", "webhooks", "fleet",
                         "identity files", "config files", "memory",
                         "backups", "templates", "personas", "secrets",
                         "logs"):
            self.assertFalse(by_name[resource]["found"],
                             f"{resource} should not be found on an empty box")
        self.assertEqual(len(rows), len(audit.CATEGORIES))


class TableShapeTests(AuditBase):
    def test_table_shape_sorted_non_empty(self):
        rows = audit.table()
        self.assertTrue(rows)
        # shape of every row
        for r in rows:
            self.assertEqual(
                sorted(r.keys()),
                sorted(["resource", "hermes", "claude", "atropos_status",
                        "recommendation", "found"]))
            self.assertIsInstance(r["resource"], str)
            self.assertIsInstance(r["hermes"], str)
            self.assertIsInstance(r["claude"], str)
            self.assertIsInstance(r["atropos_status"], str)
            self.assertIsInstance(r["recommendation"], str)
            self.assertIsInstance(r["found"], bool)
        # sorted by category
        names = [r["resource"] for r in rows]
        self.assertEqual(names, sorted(names))
        # every universal category is present (same set as categories())
        self.assertEqual(set(audit.categories()), set(names))
        for expected in ("mcp", "models", "commands", "aliases", "env",
                         "secrets", "templates", "identity files",
                         "config files", "webhooks", "cron", "skills",
                         "plugins", "hooks", "personas", "effort",
                         "routers", "backups", "alerts", "jailbreak",
                         "sessions", "marketplace", "fleet", "memory",
                         "themes", "i18n", "update", "logs", "permissions",
                         "tui"):
            self.assertIn(expected, names)

    def test_summary_counts(self):
        s = audit.summary()
        self.assertEqual(s["total"], len(audit.CATEGORIES))
        self.assertEqual(s["canonical"] + s["monitored"] + s["ignored"],
                         s["total"])
        self.assertEqual(s["found"] + s["missing"], s["total"])
        self.assertEqual(s["ignored"], 1)  # tui only
        self.assertGreater(s["canonical"], s["monitored"])

    def test_status_counts_match_table(self):
        rows = audit.table()
        s = audit.summary()
        for status in ("canonical", "monitored", "ignored"):
            self.assertEqual(
                s[status],
                sum(1 for r in rows if r["atropos_status"] == status),
                f"summary {status} count must match the table")
        self.assertEqual(
            s["found"], sum(1 for r in rows if r["found"]))

    def test_categories_are_unique(self):
        cats = audit.categories()
        self.assertEqual(len(cats), len(set(cats)))


if __name__ == "__main__":
    unittest.main()
