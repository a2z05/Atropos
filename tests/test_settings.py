#!/usr/bin/env python3
"""Atropos settings tests — schema, coercion, migration, secrets."""

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

from core import config, settings  # noqa: E402


class SettingsBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self._h = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_settings_")
        os.environ["ATROPOS_HOME"] = self.tmp
        os.environ["HERMES_HOME"] = str(Path(self.tmp) / ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, orig in (("ATROPOS_HOME", self._a), ("HERMES_HOME", self._h)):
            if orig is not None:
                os.environ[k] = orig
            else:
                os.environ.pop(k, None)


class SchemaTests(SettingsBase):
    def test_schema_has_required_groups(self):
        groups = settings.groups()
        for g in ("core", "watch", "alerts", "dashboard", "backup",
                  "guest", "skills", "jailbreak", "failover", "extensions"):
            self.assertIn(g, groups)

    def test_defaults_preserve_old_behavior(self):
        self.assertEqual(settings.get("router.active"), "nain")
        self.assertEqual(settings.get("router.model"), "deepmo")
        self.assertEqual(settings.get("watch.interval"), 1800)
        self.assertEqual(settings.get("watch.threshold_disk"), 80)
        self.assertEqual(settings.get("backup.period"), "off")
        self.assertEqual(settings.get("dashboard.port"), 8787)
        self.assertEqual(settings.get("guest.enabled"), False)
        self.assertEqual(settings.get("failover.order"), ["nain", "omni", "local"])

    def test_router_choices_only_nain_omni_local(self):
        for key in ("router.active",):
            spec = settings.schema()[key]
            self.assertEqual(sorted(spec["choices"]), ["local", "nain", "omni"])
        # failover order items validated too
        with self.assertRaises(ValueError):
            settings.set("failover.order", ["nain", "deepmo"])

    def test_effort_tiers(self):
        self.assertEqual(settings.EFFORT_TIERS,
                         ["minimal", "low", "medium", "high", "xhigh", "ultracode", "tryhard"])
        for h in ("hermes", "claude", "atropos"):
            self.assertEqual(settings.get(f"effort.{h}"), "medium")


class CoercionTests(SettingsBase):
    def test_int_coercion_from_string(self):
        settings.set("dashboard.port", "8788")
        self.assertEqual(settings.get("dashboard.port"), 8788)
        self.assertIsInstance(settings.get("dashboard.port"), int)

    def test_rejects_wrong_type(self):
        with self.assertRaises(ValueError):
            settings.set("dashboard.port", "abc")
        with self.assertRaises(ValueError):
            settings.set("watch.interval", "many seconds")
        with self.assertRaises(ValueError):
            settings.set("guest.enabled", "maybe")

    def test_bool_from_string(self):
        settings.set("guest.enabled", "true")
        self.assertIs(settings.get("guest.enabled"), True)
        settings.set("guest.enabled", "off")
        self.assertIs(settings.get("guest.enabled"), False)

    def test_unknown_key_get_none(self):
        self.assertIsNone(settings.get("does.not.exist"))

    def test_unknown_key_set_raises(self):
        with self.assertRaises(ValueError):
            settings.set("nope.key", 1)

    def test_range_validation(self):
        with self.assertRaises(ValueError):
            settings.set("dashboard.port", 70000)
        with self.assertRaises(ValueError):
            settings.set("watch.threshold_disk", 101)

    def test_readonly_rejected(self):
        with self.assertRaises(ValueError):
            settings.set("version", "9.9.9")
        with self.assertRaises(ValueError):
            settings.set("hermes.home", "/tmp/other")

    def test_choice_validation(self):
        with self.assertRaises(ValueError):
            settings.set("dashboard.theme", "neon")
        with self.assertRaises(ValueError):
            settings.set("backup.period", "hourly")
        with self.assertRaises(ValueError):
            settings.set("dashboard.accent", "pink")


class MigrationTests(SettingsBase):
    def test_legacy_thresholds_folded(self):
        cfg = config.load()
        cfg.setdefault("alerts", {})["thresholds"] = {"disk": 85, "latency_ms": 9000}
        config.save(cfg)
        settings.migrate()
        self.assertEqual(settings.get("alerts.threshold_disk"), 85)
        self.assertEqual(settings.get("alerts.latency_ms"), 9000)
        file_text = config.config_path().read_text(encoding="utf-8")
        self.assertNotIn("thresholds:", file_text)

    def test_migrate_idempotent(self):
        settings.set("dashboard.port", 9000)
        p = config.config_path()
        before = p.read_text(encoding="utf-8")
        settings.migrate()
        after = p.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertEqual(settings.get("dashboard.port"), 9000)

    def test_migrate_preserves_unknown_keys(self):
        p = config.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("custom_key: keep-me\nrouter:\n  active: nain\n", encoding="utf-8")
        settings.migrate()
        raw = config.parse_yaml(p.read_text(encoding="utf-8"))
        self.assertEqual(raw.get("custom_key"), "keep-me")


class SecretTests(SettingsBase):
    def test_mask_secrets(self):
        settings.set("alerts.token", "sekret")
        settings.set("dashboard.password", "hunter2")
        cfg = settings.load()
        masked = settings.mask_secrets(cfg)
        self.assertEqual(masked["alerts"]["token"], settings.SECRET_MASK)
        self.assertEqual(masked["dashboard"]["password"], settings.SECRET_MASK)
        # raw still has the real value
        self.assertEqual(cfg["alerts"]["token"], "sekret")

    def test_is_secret_flags(self):
        self.assertTrue(settings.is_secret("alerts.token"))
        self.assertTrue(settings.is_secret("dashboard.password"))
        self.assertFalse(settings.is_secret("dashboard.port"))

    def test_export_masks_secrets_by_default(self):
        settings.set("alerts.token", "sekret")
        yaml_text = settings.export_yaml()
        self.assertNotIn("sekret", yaml_text)
        self.assertIn(settings.SECRET_MASK, yaml_text)
        # with secrets included
        yaml_text2 = settings.export_yaml(include_secrets=True)
        self.assertIn("sekret", yaml_text2)


class ExportImportTests(SettingsBase):
    def test_export_import_roundtrip(self):
        settings.set("dashboard.port", 9123)
        settings.set("backup.period", "daily")
        yaml_text = settings.export_yaml(include_secrets=True)
        settings.set("dashboard.port", 1111)
        settings.import_yaml(yaml_text)
        self.assertEqual(settings.get("dashboard.port"), 9123)
        self.assertEqual(settings.get("backup.period"), "daily")

    def test_import_rejects_unknown_group(self):
        with self.assertRaises(ValueError):
            settings.import_yaml("evil_group:\n  x: 1\n")

    def test_import_rejects_bad_value(self):
        with self.assertRaises(ValueError):
            settings.import_yaml("dashboard:\n  port: not-an-int\n")


if __name__ == "__main__":
    unittest.main()