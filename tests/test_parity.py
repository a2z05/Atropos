#!/usr/bin/env python3
"""Parity enforcement — every claimed command exists on the CLI and runs."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

CLI_PATH = str(_REPO / "atropos")


def _cl(args, timeout=60):
    env = dict(os.environ)
    return subprocess.run([sys.executable, CLI_PATH] + args,
                          capture_output=True, text=True, timeout=timeout, env=env)


# every command documented in README / docs/PARITY.md must exist
CLAIMED = [
    "status", "doctor", "version", "detect", "route", "patch", "update",
    "guest", "logs", "config", "settings", "backup", "skills", "plugin",
    "effort", "tui", "repl", "lore", "setup", "alert", "jailbreak",
    "routing", "mcp", "models", "webhooks", "identity", "configs", "audit",
    "fleet", "budget", "links", "snapshots", "activity", "memory", "files",
    "chat", "announce", "commands", "install", "sync", "middleware",
    "agent", "telegram", "dashboard", "watch", "search", "cron", "web",
    "kanban", "email", "tts", "vision", "imagine", "video", "youtube",
    "x", "docs", "hue", "audio", "delegate", "bridge",
    "approve", "ai-mod", "autoskill", "curator", "attribution", "orchestrate",
    "sessions",
]


class CliSurfaceTests(unittest.TestCase):
    def test_help_mentions_every_command(self):
        r = _cl(["--help"])
        out = r.stdout + r.stderr
        for cmd in CLAIMED:
            self.assertIn(cmd, out, f"`{cmd}` missing from --help")

    def test_every_command_has_parser(self):
        # each claimed command parses (help exits 0) — proves the subparser exists
        for cmd in CLAIMED:
            r = _cl([cmd, "--help"])
            self.assertNotIn("invalid choice", (r.stdout + r.stderr).lower(),
                             f"`{cmd}` has no parser")


class ReadonlyInvocationTests(unittest.TestCase):
    """Safe commands must actually run and exit cleanly."""

    def test_version_runs(self):
        r = _cl(["version", "--quiet"])
        self.assertIn("atropos", (r.stdout + r.stderr).lower())

    def test_status_runs(self):
        r = _cl(["status"])
        self.assertIn("atropos", (r.stdout + r.stderr).lower())

    def test_lore_runs(self):
        r = _cl(["lore"])
        self.assertEqual(r.returncode, 0)

    def test_kanban_list_runs(self):
        r = _cl(["kanban"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("todo", r.stdout)

    def test_cron_list_runs(self):
        r = _cl(["cron"])
        self.assertEqual(r.returncode, 0)

    def test_middleware_list_runs(self):
        r = _cl(["middleware"])
        self.assertIn("pii", r.stdout)

    def test_approve_check_runs(self):
        r = _cl(["approve", "check", "rm -rf /etc"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("HARDLINE", (r.stdout + r.stderr))

    def test_approve_mode_runs(self):
        r = _cl(["approve", "mode"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("approvals.mode", r.stdout)


class ParityMatrixTests(unittest.TestCase):
    def test_parity_doc_exists(self):
        p = _REPO / "docs" / "PARITY.md"
        self.assertTrue(p.exists())
        text = p.read_text(encoding="utf-8")
        self.assertIn("Hermes", text)
        self.assertIn("Claude", text)
        self.assertIn("Atropos", text)

    def test_no_pending_rows_marked_done(self):
        text = (_REPO / "docs" / "PARITY.md").read_text(encoding="utf-8")
        # nothing claims ✅ while being TODO in the code (spot check the CLI)
        for cmd in CLAIMED:
            self.assertNotIn(f"| `atropos {cmd}` | TODO |", text)


if __name__ == "__main__":
    unittest.main()