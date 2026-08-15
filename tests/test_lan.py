#!/usr/bin/env python3
"""Atropos LAN sharing tests — ip detection, share URL, QR frame, devices."""

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

from core import config, lan  # noqa: E402


class LanBase(unittest.TestCase):
    def setUp(self):
        self._a = os.environ.get("ATROPOS_HOME")
        self.tmp = tempfile.mkdtemp(prefix="atropos_lan_")
        os.environ["ATROPOS_HOME"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._a is not None:
            os.environ["ATROPOS_HOME"] = self._a
        else:
            os.environ.pop("ATROPOS_HOME", None)


class LanIpTests(LanBase):
    def _fake_socket(self, ip):
        """A socket whose getsockname() returns the desired ip."""
        def factory(*a, **k):
            s = mock.Mock()
            s.connect = mock.Mock()
            s.close = mock.Mock()
            s.getsockname = mock.Mock(return_value=(ip, 54321))
            return s
        return factory

    def test_udp_trick_ip(self):
        with mock.patch("socket.socket", self._fake_socket("192.168.1.42")):
            self.assertEqual(lan.lan_ip(), "192.168.1.42")

    def test_fallback_to_hostname(self):
        with mock.patch("socket.socket", side_effect=OSError("no net")), \
             mock.patch("socket.gethostbyname", return_value="10.0.0.7"):
            self.assertEqual(lan.lan_ip(), "10.0.0.7")

    def test_fallback_to_loopback(self):
        with mock.patch("socket.socket", side_effect=OSError("no net")), \
             mock.patch("socket.gethostbyname", side_effect=OSError("dns down")):
            self.assertEqual(lan.lan_ip(), "127.0.0.1")

    def test_share_url_uses_dashboard_port(self):
        with mock.patch("core.lan.lan_ip", return_value="192.168.1.50"):
            self.assertEqual(lan.share_url(), "http://192.168.1.50:8787/")
            config.set_path("dashboard.port", 9001)
            self.assertEqual(lan.share_url(), "http://192.168.1.50:9001/")

    def test_qr_ascii_is_decorative_but_structured(self):
        url = "http://192.168.1.50:8787/"
        lines = lan.qr_ascii(url)
        frame = 21  # default cell size
        self.assertEqual(len(lines), frame)
        # caption sits to the right of the FIRST row: frame + pad + URL
        self.assertEqual(len(lines[0]), frame + lan._CAPTION_PAD + len(url))
        self.assertTrue(lines[0].endswith(url))
        # all remaining rows are pure square frame
        self.assertTrue(all(len(ln) == frame for ln in lines[1:]))
        self.assertTrue(all(set(ln) <= {"█", " "} for ln in lines[1:]))
        # top-left finder square visible (solid border rows at the top)
        self.assertTrue(any("███" in ln for ln in lines[:3]))
        # no data-uri or scannability claims
        self.assertNotIn("payload", "\n".join(lines).lower())
        self.assertNotIn("scan", "\n".join(lines).lower())

    def test_qr_empty_text_has_no_caption(self):
        lines = lan.qr_ascii("")
        self.assertTrue(all(len(ln) == len(lines[0]) for ln in lines))


class DeviceTests(LanBase):
    def test_touch_adds_pending(self):
        d = lan.touch("fp-1", "phone")
        self.assertFalse(d["approved"])
        self.assertEqual(d["name"], "phone")
        self.assertIn("id", d)
        pend = lan.pending_devices()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["fingerprint"], "fp-1")
        self.assertFalse(lan.is_approved("fp-1"))

    def test_approve_deny_flow(self):
        lan.touch("fp-2", "tablet")
        d = lan.pending_devices()[0]
        # deny works
        self.assertTrue(lan.deny(d["id"]))
        self.assertEqual(lan.pending_devices(), [])
        # touch again → new entry, approve it
        lan.touch("fp-2", "tablet")
        d = lan.pending_devices()[0]
        approved = lan.approve(d["id"])
        self.assertIsNotNone(approved)
        self.assertTrue(approved["approved"])
        self.assertTrue(lan.is_approved("fp-2"))
        self.assertEqual(len(lan.pending_devices()), 0)
        self.assertEqual(len(lan.known_devices()), 1)
        self.assertEqual(lan.known_devices()[0]["fingerprint"], "fp-2")

    def test_approve_unknown_returns_none(self):
        self.assertIsNone(lan.approve("nope"))

    def test_deny_unknown_false(self):
        self.assertFalse(lan.deny("nope"))

    def test_fingerprint_gating(self):
        lan.touch("fp-3", "other")
        self.assertFalse(lan.is_approved("fp-3"))
        lan.approve(lan.pending_devices()[0]["id"])
        self.assertTrue(lan.is_approved("fp-3"))
        self.assertFalse(lan.is_approved("fp-3-typo"))

    def test_touch_approved_refreshes_without_duplicate(self):
        lan.touch("fp-4", "main")
        lan.approve(lan.pending_devices()[0]["id"])
        d = lan.touch("fp-4", "main")
        self.assertTrue(d["approved"])
        self.assertEqual(len(lan.known_devices()), 1)

    def test_known_devices_newest_first(self):
        stamps = iter(["2026-08-15T10:00:00+00:00", "2026-08-15T10:00:01+00:00",
                       "2026-08-15T10:00:02+00:00", "2026-08-15T10:00:03+00:00"])
        with mock.patch("core.lan._now_iso", side_effect=lambda: next(stamps)):
            lan.touch("fp-a", "old")
            lan.touch("fp-b", "new")
            for d in lan.pending_devices():
                lan.approve(d["id"])
        known = lan.known_devices()
        self.assertEqual(len(known), 2)
        self.assertEqual(known[0]["fingerprint"], "fp-b")

    def test_rate_limit_50_devices(self):
        for i in range(60):
            lan.touch(f"fp-{i}", f"dev-{i}")
        self.assertEqual(len(lan.pending_devices()), lan.MAX_DEVICES)
        # approved devices survive the cap
        lan.touch("keep-me", "keeper")
        for d in lan.pending_devices():
            lan.approve(d["id"])
        for i in range(40):
            lan.touch(f"new-{i}", f"n-{i}")
        self.assertTrue(any(d["fingerprint"] == "keep-me" for d in lan.known_devices()))
        self.assertLessEqual(len(lan.pending_devices()) + len(lan.known_devices()),
                             lan.MAX_DEVICES)

    def test_fingerprint_required(self):
        with self.assertRaises(ValueError):
            lan.touch("   ")

    def test_persisted_to_devices_json(self):
        lan.touch("fp-5", "persist")
        p = Path(self.tmp) / "devices.json"
        self.assertTrue(p.exists())
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["fingerprint"], "fp-5")
        self.assertFalse(data[0]["approved"])

    def test_corrupt_devices_file_degrades_gracefully(self):
        Path(self.tmp).mkdir(parents=True, exist_ok=True)
        (Path(self.tmp) / "devices.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(lan.pending_devices(), [])
        self.assertEqual(lan.known_devices(), [])
        d = lan.touch("fp-6", "after-corrupt")
        self.assertFalse(d["approved"])
        self.assertEqual(len(lan.pending_devices()), 1)


if __name__ == "__main__":
    unittest.main()
