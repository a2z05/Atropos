#!/usr/bin/env python3
"""Atropos MCP registry tests — CRUD, discovery dedupe, adopt, probes."""

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

from core import config, mcp  # noqa: E402


class MCPBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_mcp_")
        self.home = Path(self.tmp)
        os.environ["ATROPOS_HOME"] = str(self.home)
        os.environ["HERMES_HOME"] = str(self.home / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)

    def registry(self) -> list:
        return json.loads((self.home / "mcp_servers.json").read_text(encoding="utf-8"))


class RegistryTests(MCPBase):
    def test_add_list_enable_disable(self):
        mcp.add("github", "stdio", "npx", ["-y", "server-github"],
                {"GITHUB_TOKEN": "ghp_1234567890"})
        entries = self.registry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "github")
        self.assertEqual(entries[0]["type"], "stdio")
        self.assertEqual(entries[0]["source"], "manual")
        self.assertTrue(entries[0]["adopted"])
        self.assertEqual(entries[0]["args"], ["-y", "server-github"])
        self.assertEqual(entries[0]["env"]["GITHUB_TOKEN"], "ghp_1234567890")

        mcp.disable("github")
        self.assertFalse(self.registry()[0]["enabled"])
        mcp.enable("github")
        self.assertTrue(self.registry()[0]["enabled"])

        names = [e["name"] for e in mcp.list_servers()]
        self.assertEqual(names, ["github"])

    def test_add_http_server(self):
        mcp.add("web", "http", url="https://example.com/mcp")
        e = self.registry()[0]
        self.assertEqual(e["type"], "http")
        self.assertEqual(e["url"], "https://example.com/mcp")
        self.assertEqual(e["command"], "")

    def test_add_duplicate_raises(self):
        mcp.add("github", "stdio", "npx")
        with self.assertRaises(ValueError):
            mcp.add("github", "stdio", "npx")

    def test_name_validation_rejects_traversal(self):
        for bad in ("../evil", "a/b", "..", ".hidden", "", "name with space",
                    "x" * 65, "a\\b"):
            with self.assertRaises(ValueError):
                mcp.add(bad, "stdio", "npx")
        self.assertEqual(self.registry(), [])

    def test_remove_trashes_entry(self):
        mcp.add("github", "stdio", "npx")
        res = mcp.remove("github")
        self.assertTrue(res["ok"])
        self.assertEqual(self.registry(), [])
        trash = self.home / "trash"
        self.assertTrue(trash.exists())
        names = [p.name for p in trash.glob("mcp-github-*.json")]
        self.assertEqual(len(names), 1)
        # trashed copy keeps the entry
        saved = json.loads((trash / names[0]).read_text(encoding="utf-8"))
        self.assertEqual(saved["name"], "github")

    def test_remove_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            mcp.remove("nope")

    def test_mode_validation(self):
        mcp.add("github", "stdio", "npx")
        mcp.mode("github", "per-harness")
        self.assertEqual(self.registry()[0]["mode"], "per-harness")
        with self.assertRaises(ValueError):
            mcp.mode("github", "everywhere")

    def test_stats(self):
        mcp.add("github", "stdio", "npx")
        mcp.add("web", "http", url="https://example.com/mcp")
        mcp.disable("web")
        s = mcp.stats()
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["enabled"], 1)
        self.assertEqual(s["per_source"], {"manual": 2})
        self.assertEqual(s["per_mode"], {"shared": 2})


class DiscoveryTests(MCPBase):
    def _write_claude_json(self):
        d = self.home / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / "mcp.json").write_text(json.dumps({
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "ghp_1234567890"},
                },
                "web": {"url": "https://example.com/mcp", "type": "http"},
            }
        }), encoding="utf-8")

    def _write_hermes_cfg(self):
        h = self.home / ".hermes"
        h.mkdir(parents=True, exist_ok=True)
        (h / "config.yaml").write_text(config.dump_yaml({
            "mcp": {
                "servers": {
                    "search": {
                        "command": "python",
                        "args": ["search.py"],
                        "env": {"API_KEY": "sk-abcdefgh"},
                    }
                }
            }
        }) + "\n", encoding="utf-8")

    def test_rescan_discovers_and_adds(self):
        self._write_claude_json()
        self._write_hermes_cfg()
        r = mcp.rescan()
        self.assertEqual(r["found"], ["github", "search", "web"])
        self.assertEqual(r["added"], ["github", "search", "web"])
        self.assertEqual(r["skipped"], [])
        entries = self.registry()
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["github"]["source"], "claude")
        self.assertEqual(by_name["search"]["source"], "hermes")
        self.assertEqual(by_name["web"]["type"], "http")
        # ask-first gate: adopted stays False until adopt()
        self.assertFalse(by_name["github"]["adopted"])

    def test_rescan_dedupes_by_name(self):
        self._write_claude_json()
        mcp.rescan()
        mcp.adopt(["github"])
        r = mcp.rescan()  # second pass must not duplicate
        self.assertEqual(r["found"], ["github", "web"])
        self.assertEqual(r["added"], [])
        self.assertEqual(r["skipped"], ["github", "web"])
        entries = self.registry()
        self.assertEqual(len([e for e in entries if e["name"] == "github"]), 1)

    def test_adopt_flow(self):
        self._write_claude_json()
        mcp.rescan()
        res = mcp.adopt(["github"])
        self.assertEqual(res["adopted"], ["github"])
        entries = {e["name"]: e for e in self.registry()}
        self.assertTrue(entries["github"]["adopted"])
        self.assertFalse(entries["web"]["adopted"])
        # adopting again is a no-op skip
        res2 = mcp.adopt(["github"])
        self.assertEqual(res2["adopted"], [])
        self.assertEqual(res2["skipped"], ["github"])

    def test_adopt_all(self):
        self._write_claude_json()
        mcp.rescan()
        res = mcp.adopt()
        self.assertEqual(sorted(res["adopted"]), ["github", "web"])

    def test_rescan_bad_files_do_not_crash(self):
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "mcp.json").write_text("{not json", encoding="utf-8")
        h = self.home / ".hermes"
        h.mkdir(parents=True, exist_ok=True)
        (h / "config.yaml").write_text("mcp: [broken", encoding="utf-8")
        r = mcp.rescan()
        self.assertEqual(r["found"], [])
        self.assertEqual(r["added"], [])


class ProbeTests(MCPBase):
    def test_probe_failure_does_not_raise(self):
        # stdio probe of a missing executable must not raise
        mcp.add("ghost", "stdio", "definitely-not-a-real-binary-xyz")
        st = mcp.status("ghost")
        self.assertFalse(st["ok"])
        self.assertIn("error", st)

    def test_probe_disabled_server(self):
        mcp.add("web", "http", url="https://example.com/mcp")
        mcp.disable("web")
        st = mcp.status("web")
        self.assertFalse(st["ok"])
        self.assertEqual(st["error"], "disabled")

    def test_probe_unknown_server(self):
        st = mcp.status("nope")
        self.assertFalse(st["ok"])

    def test_probe_http_ok(self):
        with unittest.mock.patch("core.mcp.urllib.request.urlopen") as urlopen:
            fake = unittest.mock.MagicMock()
            fake.status = 200
            fake.__enter__.return_value = fake
            urlopen.return_value = fake
            mcp.add("web", "http", url="https://example.com/mcp")
            st = mcp.status("web")
        self.assertTrue(st["ok"])
        self.assertEqual(st["status_code"], 200)

    def test_probe_http_500_fails(self):
        import urllib.error
        with unittest.mock.patch("core.mcp.urllib.request.urlopen",
                                 side_effect=urllib.error.HTTPError(
                                     "https://example.com/mcp", 503, "down", None, None)):
            mcp.add("web", "http", url="https://example.com/mcp")
            st = mcp.status("web")
        self.assertFalse(st["ok"])
        self.assertEqual(st["status_code"], 503)

    def test_test_alias(self):
        mcp.add("web", "http", url="https://example.com/mcp")
        with unittest.mock.patch("core.mcp.urllib.request.urlopen",
                                 side_effect=OSError("refused")):
            st = mcp.test("web")
        self.assertFalse(st["ok"])


class ProjectionTests(MCPBase):
    def test_project_to_claude_merges_and_redacts_secrets(self):
        mcp.add("github", "stdio", "npx", ["-y", "server-github"],
                {"GITHUB_TOKEN": "ghp_1234567890", "GITHUB_USER": "octocat"})
        res = mcp.project_to_harness("github", "claude")
        self.assertTrue(res["ok"])
        p = self.home / ".claude" / "mcp.json"
        self.assertTrue(p.exists())
        data = json.loads(p.read_text(encoding="utf-8"))
        spec = data["mcpServers"]["github"]
        self.assertEqual(spec["command"], "npx")
        # token value replaced by a placeholder, never plaintext
        self.assertEqual(spec["env"]["GITHUB_TOKEN"], "{ref:secrets.json:GITHUB_TOKEN}")
        self.assertNotIn("ghp_1234567890", p.read_text(encoding="utf-8"))
        # non-secret env value passes through untouched
        self.assertEqual(spec["env"]["GITHUB_USER"], "octocat")

    def test_project_to_hermes(self):
        mcp.add("search", "stdio", "python", ["search.py"],
                {"API_KEY": "sk-abcdefgh12345"})
        mcp.project_to_harness("search", "hermes")
        p = self.home / ".hermes" / "config.yaml"
        self.assertTrue(p.exists())
        data = config.parse_yaml(p.read_text(encoding="utf-8"))
        spec = data["mcp"]["servers"]["search"]
        self.assertEqual(spec["command"], "python")
        self.assertEqual(spec["env"]["API_KEY"], "{ref:secrets.json:API_KEY}")
        self.assertNotIn("sk-abcdefgh12345", p.read_text(encoding="utf-8"))

    def test_project_rejects_bad_harness(self):
        mcp.add("github", "stdio", "npx")
        with self.assertRaises(ValueError):
            mcp.project_to_harness("github", "openai")


if __name__ == "__main__":
    unittest.main()
