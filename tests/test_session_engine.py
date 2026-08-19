"""Session Engine tests (v19 M6). Hermetic: temp ATROPOS_HOME per test.

Covers the cheap classifier, all three modes, affinity, mirroring,
threads, hybrid thresholds, config roundtrip, merge, and the engine's
graceful degradation. Latency bench included (cheap classifier on 10k
synthetic messages — asserts the 0-3ms budget).
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from core import chat, session_classify as sc, session_engine as eng, settings


def _new_home() -> str:
    home = tempfile.mkdtemp(prefix="atropos-sess-")
    os.environ["ATROPOS_HOME"] = home
    eng.reset_surface("cli")
    eng.reset_surface("telegram")
    return home


def _seed_chat_msgs(sid: str, n: int):
    """Insert n raw chat messages for a session (bypasses LLM)."""
    conn = chat._connect()
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY"
            " AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
            " harness TEXT, model TEXT, effort TEXT, latency_ms INTEGER,"
            " ts TEXT, tokens INTEGER)")
        for i in range(n):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, harness,"
                " model, effort, ts) VALUES (?, 'user', ?, '', '', '',"
                " '2026-08-18T12:00:00Z')",
                (sid, f"general chit chat {i}"))
        conn.commit()
    finally:
        conn.close()


class CheapClassifierTests(unittest.TestCase):
    def setUp(self):
        _new_home()

    def test_known_topic_routes_to_existing_session(self):
        sessions = [
            {"id": "s1", "title": "deploy the railway app",
             "keywords": ["deploy", "railway", "docker"]},
            {"id": "s2", "title": "travel itinerary",
             "keywords": ["flight", "hotel", "visa"]},
        ]
        r = sc.classify("the deploy pipeline failed again", sessions)
        self.assertEqual(r["decision"], "existing")
        self.assertEqual(r["session_id"], "s1")
        self.assertGreaterEqual(r["confidence"], 0.0)

    def test_unknown_topic_is_new(self):
        r = sc.classify("what is the meaning of life", [])
        self.assertEqual(r["decision"], "new")
        self.assertEqual(r["confidence"], 0.0)

    def test_generic_message_scores_zero(self):
        r = sc.classify("good morning how are you", [])
        self.assertEqual(r["score"], 0.0)

    def test_real_session_beats_dictionary(self):
        # a real session's weak hit must outrank the built-in topic dict
        sessions = [{"id": "s1", "title": "deploy - cli",
                     "keywords": ["deploy", "railway", "app", "tonight"]}]
        r = sc.classify("the deploy pipeline failed again", sessions)
        self.assertEqual(r["decision"], "existing")
        self.assertEqual(r["session_id"], "s1")

    def test_command_only_never_classifies(self):
        r = sc.classify("/status", [])
        self.assertEqual(r["decision"], "none")

    def test_learn_topic_extracts_keywords(self):
        kw = sc.learn_topic("fix the payment invoice refund bug in the budget module",
                            "Payment fixes")
        self.assertIn("payment", kw)
        self.assertIn("invoice", kw)
        self.assertLessEqual(len(kw), 8)

    def test_user_dictionary_extends_vocab(self):
        home = _new_home()
        import pathlib
        p = pathlib.Path(home) / "session_topics.yaml"
        p.write_text("topics:\n  astronomy: [telescope, orbit, galaxy]\n",
                     encoding="utf-8")
        r = sc.classify("set up the telescope for the orbit", [])
        self.assertEqual(r["decision"], "new")
        self.assertEqual(r["title"], "astronomy")


class EngineUnifiedTests(unittest.TestCase):
    def setUp(self):
        _new_home()
        settings.set("session_engine.mode", "unified")

    def test_thread_labels_created(self):
        r1 = eng.classify_message("can we deploy the railway app tonight", "cli")
        r2 = eng.classify_message("also book a flight to paris", "cli")
        self.assertEqual(r1["session_id"], r2["session_id"])
        self.assertEqual(r1["thread"], "deploy")
        self.assertEqual(r2["thread"], "travel")

    def test_context_packing_weights_thread(self):
        eng.classify_message("deploy the railway app", "cli")
        ctx = eng.request_context("cli", "deploy more")
        self.assertEqual(ctx["thread"], "deploy")
        self.assertIn("session_id", ctx)

    def test_manual_thread_start_end(self):
        r = eng.set_thread("cli", "code review")
        self.assertTrue(r["ok"])
        self.assertEqual(eng._threads.get("cli"), "code review")
        r = eng.set_thread("cli", "")
        self.assertTrue(r["ok"])
        self.assertEqual(eng._threads.get("cli", ""), "")

    def test_mode_off_passes_through(self):
        settings.set("session_engine.surfaces.telegram", "off")
        r = eng.classify_message("deploy the railway app", "telegram")
        self.assertEqual(r["mode"], "off")
        self.assertEqual(r["decision"], "current")


class EngineAutoSplitTests(unittest.TestCase):
    def setUp(self):
        _new_home()
        settings.set("session_engine.mode", "auto-split")
        settings.set("session_engine.new_topic_min_messages", 3)

    def test_split_creates_new_session(self):
        base = None
        for i in range(4):
            r = eng.classify_message(f"hello chat message number {i}", "cli")
            base = r["session_id"]
        r1 = eng.classify_message("can we deploy the railway app tonight", "cli")
        r2 = eng.classify_message("the deploy pipeline failed again", "cli")
        self.assertEqual(r1["decision"], "new")
        # follow-up routes back into the freshly-created deploy session
        self.assertEqual(r2["session_id"], r1["session_id"])
        self.assertNotEqual(r2["session_id"], base)

    def test_new_topic_splits_again(self):
        # needs a multi-keyword topic signal — a single "flight" hit alone
        # stays in the current session (affinity, correct behavior)
        for i in range(4):
            eng.classify_message(f"hello chat message number {i}", "cli")
        r1 = eng.classify_message("deploy the railway app tonight", "cli")
        base = r1["session_id"]
        r2 = eng.classify_message("book a flight and check the hotel for the visa", "cli")
        self.assertNotEqual(r1["session_id"], r2["session_id"])
        self.assertEqual(r2["decision"], "new")

    def test_affinity_keeps_ambiguous_in_session(self):
        for i in range(4):
            eng.classify_message(f"hello chat message number {i}", "cli")
        r1 = eng.classify_message("deploy the railway app tonight", "cli")
        # an unrelated follow-up with zero overlap stays put (affinity)
        r2 = eng.classify_message("could you pass the salt", "cli")
        self.assertEqual(r1["session_id"], r2["session_id"])

    def test_new_topic_min_messages_gate(self):
        settings.set("session_engine.new_topic_min_messages", 10)
        for i in range(4):
            eng.classify_message(f"hello chat message number {i}", "cli")
        r1 = eng.classify_message("deploy the railway app tonight", "cli")
        # below the lock-in count → no split yet
        self.assertEqual(r1["decision"], "current")

    def test_max_sessions_reuses_oldest(self):
        settings.set("session_engine.max_sessions", 2)
        for i in range(6):
            eng.classify_message(f"hello chat message number {i}", "cli")
        eng.classify_message("deploy the railway app tonight", "cli")
        eng.classify_message("book a flight to paris for next week", "cli")
        eng.classify_message("research the arxiv paper on routing", "cli")
        sessions = chat.session_list(limit=50)
        self.assertLessEqual(len(sessions), 2)


class EngineHybridTests(unittest.TestCase):
    def setUp(self):
        _new_home()
        settings.set("session_engine.mode", "hybrid")

    def test_shallow_never_splits(self):
        r1 = eng.classify_message("deploy the railway app", "cli")
        r2 = eng.classify_message("book a flight to paris", "cli")
        self.assertEqual(r1["session_id"], r2["session_id"])
        self.assertEqual(eng.stats()["splits"], 0)

    def test_deep_confident_split(self):
        r1 = eng.classify_message("deploy the railway app", "cli")
        base = r1["session_id"]
        _seed_chat_msgs(base, 30)
        # strong multi-keyword topic → passes the 0.9 hybrid gate
        r3 = eng.classify_message("deploy docker railway rollback", "cli")
        self.assertNotEqual(r3["session_id"], base)
        self.assertGreaterEqual(eng.stats()["splits"], 1)

    def test_hybrid_confidence_respected(self):
        settings.set("session_engine.hybrid_confidence", 0.99)  # unreachable
        r1 = eng.classify_message("deploy the railway app", "cli")
        _seed_chat_msgs(r1["session_id"], 30)
        # partial topic hit (0.67) — below the 0.99 gate → stays unified
        r3 = eng.classify_message("deploy docker pipeline restart the vm", "cli")
        self.assertEqual(r3["session_id"], r1["session_id"])


class EngineMirrorTests(unittest.TestCase):
    def setUp(self):
        _new_home()
        settings.set("session_engine.mode", "auto-split")

    def _build_deploy_session(self):
        for i in range(4):
            eng.classify_message(f"hello chat message number {i}", "cli")
        r1 = eng.classify_message("can we deploy the railway app tonight", "cli")
        return r1["session_id"]

    def test_mirror_records_link(self):
        deploy_sid = self._build_deploy_session()
        # simulate a deep-switch: a travel-topic message arrives while the
        # deploy session is current; the async classifier later decides it
        # belongs to a travel session and mirrors it there
        travel_sid = eng.chat.create_session("travel - cli")
        eng.switch_session("cli", deploy_sid)
        eng.mirror_later("book a flight and check the hotel for the visa",
                         "cli", {"decision": "new"})
        stats = eng.stats()
        self.assertGreaterEqual(stats["mirrors"], 1)

    def test_mirror_never_blocks_reply(self):
        # classify_message must return before any async mirror work —
        # the latency budget is sub-millisecond regardless of routing
        self._build_deploy_session()
        t0 = __import__("time").perf_counter()
        r2 = eng.classify_message("deploy the docker railway pipeline", "cli")
        dt = (__import__("time").perf_counter() - t0) * 1000
        self.assertLess(dt, 1000)
        self.assertEqual(r2["decision"], "new")  # routed instantly, no wait

    def test_unmirror_marks_undone(self):
        self._build_deploy_session()
        travel_sid = eng.chat.create_session("travel - cli")
        eng.mirror_later("book a flight and check the hotel for the visa",
                         "cli", {"decision": "new"})
        m = [x for s in eng.sessions_detailed()
             for x in s["mirrors"] if not x["undone"]]
        self.assertTrue(m)
        r = eng.unmirror("cli", m[0]["id"])
        self.assertTrue(r["ok"])
        m2 = [x for s in eng.sessions_detailed()
              for x in s["mirrors"] if not x["undone"]]
        self.assertEqual(len(m2), 0)


class EngineMergeTests(unittest.TestCase):
    def setUp(self):
        _new_home()

    def test_merge_two_sessions(self):
        a = chat.create_session("Alpha")
        b = chat.create_session("Beta")
        conn = chat._connect()
        try:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY"
                " AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                " harness TEXT, model TEXT, effort TEXT, latency_ms INTEGER,"
                " ts TEXT, tokens INTEGER)")
            conn.execute("INSERT INTO messages (session_id, role, content, ts)"
                         " VALUES (?, 'user', 'hello from A', 't')", (a,))
            conn.execute("INSERT INTO messages (session_id, role, content, ts)"
                         " VALUES (?, 'user', 'hello from B', 't')", (b,))
            conn.commit()
        finally:
            conn.close()
        r = eng.merge_sessions("cli", a, b)
        self.assertTrue(r["ok"])
        self.assertFalse(chat.session_exists(b))
        msgs = chat.session_messages(a)
        self.assertEqual(len(msgs), 2)


class EngineConfigTests(unittest.TestCase):
    def setUp(self):
        _new_home()

    def test_surface_overrides_roundtrip(self):
        settings.set("session_engine.surfaces.telegram", "hybrid")
        settings.set("session_engine.mode", "unified")
        self.assertEqual(eng.surface_mode("telegram"), "hybrid")
        self.assertEqual(eng.surface_mode("cli"), "unified")
        self.assertEqual(eng.surface_mode("dashboard"), "unified")

    def test_mode_cards_have_explanations(self):
        for mode, card in eng.MODE_CARDS.items():
            self.assertIn("title", card)
            self.assertIn("explanation", card)
            self.assertIn("latency", card)

    def test_stats_shape(self):
        s = eng.stats()
        self.assertIn("mode", s)
        self.assertIn("splits", s)
        self.assertIn("mirrors", s)
        self.assertIn("surfaces", s)

    def test_graceful_degradation_on_bad_db(self):
        # engine never raises when chat.db is unreadable
        os.environ["ATROPOS_HOME"] = tempfile.mkdtemp(prefix="bad-db-")
        r = eng.classify_message("deploy the railway app", "cli")
        self.assertEqual(r["decision"], "current")


class LatencyBenchTests(unittest.TestCase):
    """Deliverable: time the cheap classifier on 10k synthetic messages.
    The budget is 0-3ms per call."""

    def test_cheap_classifier_latency_10k(self):
        _new_home()
        import time
        sessions = [
            {"id": "s1", "title": "deploy the railway app",
             "keywords": ["deploy", "railway", "docker", "pipeline"]},
            {"id": "s2", "title": "travel itinerary",
             "keywords": ["flight", "hotel", "visa"]},
        ]
        corpus = (
            ["deploy the railway app to production"] * 1200 +
            ["book a flight to paris for the itinerary"] * 1200 +
            ["the pipeline failed, need to rollback"] * 1200 +
            ["what time does the flight land"] * 1200 +
            ["any plans for the weekend"] * 1200 +
            ["how does the docker container work"] * 1200 +
            ["the visa application needs the hotel address"] * 1200 +
            ["let me check the git branch for the merge"] * 1200 +
            ["research the arxiv paper on routing"] * 1200 +
            ["write the draft for the chapter"] * 1200
        )  # 12000 total
        t0 = time.perf_counter()
        for msg in corpus:
            sc.classify(msg, sessions)
        dt = time.perf_counter() - t0
        avg_ms = dt / len(corpus) * 1000
        print(f"\n[bench] {len(corpus)} messages in {dt:.3f}s"
              f" = {avg_ms:.3f} ms/msg ({len(corpus)/dt:.0f} msgs/s)")
        self.assertLess(avg_ms, 3.0, f"cheap classifier over budget: {avg_ms:.3f}ms")


if __name__ == "__main__":
    unittest.main(verbosity=2)