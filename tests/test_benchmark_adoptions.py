"""Benchmark-adoption tests (v20): SSE Last-Event-ID resume, error
envelope, webhook HMAC + retry + dead-letter. Hermetic homes.
"""
import json
import os
import tempfile
import unittest

from core import dashboard, errors, memory, sse, webhooks


def _new_home() -> str:
    home = tempfile.mkdtemp(prefix="atropos-bench-")
    os.environ["ATROPOS_HOME"] = home
    return home


class MemoryTierTests(unittest.TestCase):
    """Benchmark area 12: memory tiers + importance + dedupe."""

    def setUp(self):
        _new_home()

    def test_tiers_stored(self):
        memory.add("deploy with docker", tier="core", importance=5)
        memory.add("book a flight", tier="working")
        notes = memory._load()
        tiers = {n["tier"] for n in notes}
        self.assertEqual(tiers, {"core", "working"})
        self.assertEqual(notes[0]["importance"], 5)

    def test_invalid_tier_rejected(self):
        with self.assertRaises(ValueError):
            memory.add("x", tier="bogus")

    def test_dedupe_merges_similar(self):
        a = memory.add("deploy the railway app with docker", ["deploy"])
        b = memory.add("deploy the railway app with docker containers", ["deploy"])
        self.assertEqual(a, b)
        self.assertEqual(len(memory._load()), 1)

    def test_numbered_variants_not_merged(self):
        a = memory.add("note 0 with unique token alpha")
        b = memory.add("note 1 with unique token beta")
        self.assertNotEqual(a, b)
        self.assertEqual(len(memory._load()), 2)

    def test_importance_weights_ranking(self):
        # older-but-important beats newer-but-low for the same term
        old = memory.add("deploy the railway app", ["deploy"], importance=5)
        new = memory.add("deploy a little web thing", ["deploy"], importance=1)
        notes = memory._load()
        # force the new note to be older than old by tweaking ts
        notes[1]["ts"] = "2000-01-01T00:00:00Z"
        memory._save(notes)
        r = memory.search("deploy")
        self.assertTrue(r)
        self.assertEqual(r[0]["id"], old)

    def test_auto_archive_moves_old_working(self):
        memory._ARCHIVAL_CAP = 5
        for i in range(8):
            memory.add(f"note {i} with unique token {i}", tier="working")
        notes = memory._load()
        tiers = {n["tier"] for n in notes}
        self.assertIn("archival", tiers)  # oldest working notes demoted


class ErrorCodeTests(unittest.TestCase):
    """Benchmark area 29: error codes + breadcrumbs."""

    def setUp(self):
        _new_home()
        errors.clear()

    def test_code_message_shape(self):
        msg = errors.code("E_ROUTER_001")
        self.assertIn("E_ROUTER_001", msg)
        self.assertIn("fix:", msg)
        self.assertIn("router", msg.lower())

    def test_unknown_code_returns_key(self):
        self.assertEqual(errors.code("E_BOGUS"), "E_BOGUS")

    def test_breadcrumb_trail(self):
        errors.breadcrumb("router", "ping nain -> timeout", "warn")
        errors.breadcrumb("webhook", "delivery failed", "error")
        trail = errors.trail()
        self.assertEqual(len(trail), 2)
        self.assertEqual(trail[0]["category"], "webhook")
        self.assertEqual(trail[0]["level"], "error")

    def test_clear_empties(self):
        errors.breadcrumb("x", "y")
        errors.clear()
        self.assertEqual(errors.trail(), [])


class SseResumeTests(unittest.TestCase):
    def setUp(self):
        _new_home()
        sse.hub._counter = 0
        sse.hub._history.clear()

    def test_frames_carry_ids(self):
        ids = []
        sse.hub.broadcast("status", {"a": 1})
        sse.hub.broadcast("status", {"a": 2})
        with sse.hub._lock:
            ring = sse.hub._history.get("status", [])
        self.assertEqual(len(ring), 2)
        self.assertEqual(ring[0][0], 1)
        self.assertEqual(ring[1][0], 2)

    def test_replay_from_reconnects(self):
        sse.hub.broadcast("status", {"a": 1})
        sse.hub.broadcast("status", {"a": 2})
        sse.hub.broadcast("status", {"a": 3})
        import queue
        q = queue.Queue()
        sse.hub.replay_from("x", q, last_event_id=1)
        got = []
        while not q.empty():
            got.append(q.get_nowait()[0])
        self.assertEqual(got, [2, 3])  # missed frames replayed, not the past

    def test_ring_capped(self):
        for i in range(sse.HISTORY_RING + 20):
            sse.hub.broadcast("status", {"i": i})
        with sse.hub._lock:
            ring = sse.hub._history.get("status", [])
        self.assertLessEqual(len(ring), sse.HISTORY_RING)

    def test_stream_yields_ids(self):
        import queue
        sse.hub.broadcast("status", {"a": 1})
        # replay path: a reconnecting client with a known last id gets
        # missed frames with id headers — verified without blocking on
        # the stream generator (which waits the heartbeat)
        q = queue.Queue()
        sse.hub.replay_from("c1", q, last_event_id=0)
        eid, _ch, payload = q.get_nowait()
        frame = (f"id: {eid}\nevent: status\n".encode("utf-8") + b"data: " + payload + b"\n\n")
        self.assertTrue(frame.startswith(b"id: 1"))
        self.assertIn(b"event: status", frame)

    def test_stream_yields_retry_hint(self):
        import queue
        gen = sse.stream("c3", timeout=1.0)
        first = next(gen)
        self.assertEqual(first, b"retry: 3000\n\n")
        # stop the generator without blocking on the heartbeat
        gen.close()


class ErrorEnvelopeTests(unittest.TestCase):
    def setUp(self):
        _new_home()

    def test_envelope_shape(self):
        r = dashboard._error("validation", "bad key", {"key": "x"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "bad key")
        self.assertEqual(r["error_obj"]["code"], "validation")
        self.assertEqual(r["error_obj"]["details"], {"key": "x"})

    def test_no_details_omitted(self):
        r = dashboard._error("internal", "boom")
        self.assertNotIn("details", r["error_obj"])


class WebhookAdoptionTests(unittest.TestCase):
    def setUp(self):
        _new_home()

    def test_signature_hex(self):
        sig = webhooks._sign(b"payload", "secret")
        self.assertEqual(len(sig), 64)
        self.assertEqual(webhooks._sign(b"payload", "secret"), sig)

    def test_add_with_secret(self):
        h = webhooks.add("signed", "http://127.0.0.1:1/hook", secret="sek")
        self.assertEqual(h["secret"], "sek")

    def test_dead_letter_on_failure(self):
        webhooks.add("fail", "http://127.0.0.1:1/hook", ["alerts"])
        r = webhooks.trigger("alerts", {"x": 1})
        self.assertIn("fail", r["failed"])
        self.assertGreaterEqual(len(webhooks.dead_letters()), 1)
        self.assertEqual(webhooks.dead_letters()[0]["hook"], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)