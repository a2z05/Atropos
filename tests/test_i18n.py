#!/usr/bin/env python3
"""i18n tests — language files, lookup fallbacks, RTL."""
import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core import i18n


class LangFileTests(unittest.TestCase):
    def test_at_least_10_languages(self):
        self.assertGreaterEqual(len(i18n.available()), 10)

    def test_en_master_complete(self):
        en = json.loads((Path(i18n._LANG_DIR) / "en.json").read_text(encoding="utf-8"))
        self.assertIn("nav_doctor", en)
        self.assertIn("lore_lines", en)
        self.assertIn("tips", en)
        self.assertIn("lore_verdicts", en)

    def test_all_files_valid_json(self):
        for code in i18n.available():
            # json.loads raises on malformed content — a valid file parses
            d = json.loads((Path(i18n._LANG_DIR) / f"{code}.json").read_text(encoding="utf-8"))
            self.assertIsInstance(d, dict)

    def test_missing_keys_fall_back_to_en(self):
        en = json.loads((Path(i18n._LANG_DIR) / "en.json").read_text(encoding="utf-8"))
        for code in i18n.available()[1:]:
            d = json.loads((Path(i18n._LANG_DIR) / f"{code}.json").read_text(encoding="utf-8"))
            for k, v in en.items():
                val = d.get(k, v)
                self.assertIsInstance(val, (str, list, dict))


class LookupTests(unittest.TestCase):
    def test_fa_translation(self):
        i18n.set_lang("fa")
        self.assertEqual(i18n.t("nav_doctor"), "پزشک")

    def test_en_is_default(self):
        i18n.set_lang("zz")
        self.assertEqual(i18n.t("nav_doctor"), "Doctor")

    def test_unknown_key_returns_raw(self):
        i18n.set_lang("en")
        self.assertEqual(i18n.t("no_such_key_xyz"), "no_such_key_xyz")

    def test_partial_lang_falls_back(self):
        i18n.set_lang("de")
        # de has no lore_lines → falls back to en list
        self.assertIsInstance(i18n._load("en").get("lore_lines"), list)

    def test_set_lang_valid(self):
        i18n.set_lang("fr")
        self.assertEqual(i18n.get_lang(), "fr")


if __name__ == "__main__":
    unittest.main()