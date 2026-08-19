"""Cheap session classifier — keyword/pattern scoring, no LLM, no network.

The "cheap classifier" of the Single Session Engine (v19 M2): decides, in
0-3ms, whether an incoming message continues the current session, belongs
to an existing session, or starts a new topic. Not a language model: it
scores token overlap against session titles + topic keywords + a global
stopword list, then reports the best candidate with a confidence 0..1.

Design (deviations from the v19 brief are deliberate and documented):
  * Scores are token-overlap based (stemmed word + phrase n-grams), not
    full regex intent matching — regex tables don't generalize, word
    overlap does, and it stays pure-stdlib.
  * Synthetic vocabulary is seeded from the deliverable requirement
    (10k synthetic messages) — see ``VOCAB`` below. Real session topics
    come from the engine at runtime and are stored in ``session_meta``
    (see core/session_engine.py).
  * ``session_topics.yaml`` in the atropos home is user-extendable and
    loaded lazily every call so edits apply immediately (diffable).

Public API:
    classify(text, sessions, *, surface="cli") -> dict
    score(text, title, keywords=()) -> float   # 0..1 overlap score
    learn_topic(text, title) -> list[str]       # extracted keyword list
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import detect

# ── tokenization ──────────────────────────────────────────────────────────
_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
# accented letters fold to their ASCII base (fa/de/fr/tr…):
_FOLD = {
    ord("ä"): "a", ord("ö"): "o", ord("ü"): "u", ord("é"): "e",
    ord("è"): "e", ord("ê"): "e", ord("î"): "i", ord("ï"): "i",
    ord("ç"): "c", ord("ñ"): "n", ord("ß"): "ss", ord("ø"): "o",
    ord("å"): "a", ord("æ"): "ae", ord("œ"): "oe", ord("ş"): "s",
    ord("ğ"): "g", ord("ı"): "i", ord("ć"): "c", ord("ż"): "z",
    ord("ł"): "l", ord("č"): "c", ord("š"): "s", ord("ž"): "z",
}
_ENG_STOP = frozenset(
    "a an the is are was were be been being am do does did will would "
    "can could shall should may might must have has had having of in on "
    "at to for with by from as about into over under after before during "
    "i you he she it we they me him her us them my your his its our their "
    "this that these those and but or nor not no yes so than then now "
    "what which who whom whose when where why how all any both each few "
    "more most other some such only own same too very just also still "
    "even often never always here there one two three first last next "
    "please tell let get want need help make made make did going go come "
    "know think see look like really yeah ok okay thanks thank hi hello"
    .split()
)
# non-English stopwords (fa + common de/fr/tr/es/ru/ar fillers) — the
# balance is handled by the overlap ratio, so this list can stay small.
_NON_ENG_STOP = frozenset(
    "the und der die das ein eine und ich du er sie es wir ihr mich dich "
    "auf für mit nach von zu bei aus als wenn weil dass nicht gut sehr "
    "le la les un une de des et est sont pour dans avec sur ce cette "
    "il elle nous vous être avoir mais ou dont qui que quoi "
    "el la los las un una de del y o es son esta este para con por "
    "ve ya bir bu ne ben sen biz o onlar için ile de da mi mu "
    "в и не на что это как он она они мы вы есть был была где когда "
    "ال من في على ان هذا هذه انه هي هم نحن انتم ما الذي كيف لماذا "
    .split()
)
_COMMAND_WORDS = frozenset(("/help", "/status", "/session", "/thread", "/end",
                            "/new", "/sessions", "/ops", "/doctor", "/settings"))
# messages entirely made of command words are not topics — only actual
# slash-commands (a bare word like "deploy" IS a topic signal)
_is_cmd_only = re.compile(r"^[/][a-z-]*(?:[ /][a-z-]*)*$", re.IGNORECASE).match

# ── synthetic-vocabulary seed (benchmark spec: "10k synthetic messages") ──
# 24 base words across 8 plausible topic groups. The engine's real topics
# always override these; they exist so a fresh install can already route
# "deploy the railway app" to a Railway session without any history.
VOCAB: dict[str, tuple[str, ...]] = {
    "deploy":   ("deploy", "release", "pipeline", "docker", "railway", "kubernetes", "rollback"),
    "code":     ("code", "bug", "refactor", "review", "debug", "branch", "merge"),
    "research": ("research", "paper", "source", "cite", "journal", "arxiv"),
    "writing":  ("write", "draft", "document", "prose", "edit", "chapter"),
    "ops":      ("ops", "monitor", "alert", "uptime", "incident", "oncall"),
    "design":   ("design", "ui", "theme", "panel", "layout", "component"),
    "finance":  ("invoice", "budget", "tax", "payment", "expense", "refund"),
    "travel":   ("trip", "flight", "hotel", "visa", "itinerary", "booking"),
}


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with diacritic folding; drop stopwords and
    command words. CJK text (no spaces) is split per character so a 2+
    char run can still match titles."""
    text = (text or "").translate(_FOLD).lower()
    words = [w for w in _WORD_RE.findall(text) if w]
    if not words and re.search(r"[一-鿿]", text):
        words = list(re.sub(r"[^一-鿿]", "", text))
    return [w for w in words
            if w not in _ENG_STOP and w not in _NON_ENG_STOP
            and not w.startswith("/")]


def _ngrams(tokens: list[str], n: int = 2) -> list[str]:
    """Word n-grams (n=2) — catches phrases like ``deploy railway`` that
    single tokens miss."""
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def score(text: str, title: str, keywords=()) -> float:
    """Coverage score 0..1 between a message and a session's identity.

    = (matched tokens + phrase boosts) / message tokens. A score of 1
    means every contentful word of the message is a known session word;
    0 means no overlap at all. Matching against a broad session identity
    scores higher than against a narrow one, so a 12-word message that
    hits 3 known words routes to that session while a 3-word message
    needs stronger overlap before we trust it."""
    msg_toks = tokenize(text)
    if not msg_toks:
        return 0.0
    known = set(tokenize(title)) | set(keywords or ())
    title_bigrams = set(_ngrams(tokenize(title)))
    if not known and not title_bigrams:
        return 0.0
    hit = sum(1 for w in msg_toks if w in known)
    hit += sum(1 for g in _ngrams(msg_toks) if g in title_bigrams)
    return min(1.0, hit / len(msg_toks))


# ── topic dictionary (user-extendable) ────────────────────────────────────
def dictionary_path() -> Path:
    return detect.atropos_home() / "session_topics.yaml"


def _load_dictionary() -> dict[str, list[str]]:
    """Keyword dictionary: built-in VOCAB + user ``session_topics.yaml``.

    YAML shape (subset, parsed leniently — no pip):
        topics:
          deploy: [deploy, pipeline, railway]
    Missing/empty files are fine (built-ins remain).
    """
    out: dict[str, list[str]] = {}
    for topic, words in VOCAB.items():
        out[topic] = list(words)
    p = dictionary_path()
    try:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-", "topics:", " ")):
                    continue
                if ":" not in line:
                    continue
                key, _, rest = line.partition(":")
                key = key.strip().strip("'\"")
                words = [w.strip().strip("'\"") for w in rest.split(",") if w.strip()]
                if key and words:
                    out[key] = words
    except OSError:
        pass
    return out


def learn_topic(text: str, title: str) -> list[str]:
    """Extract keywords from a new session's first message (dictionary
    growth, v19 M4). Returns the top contentful tokens (≤8)."""
    toks = tokenize(text)
    if not toks:
        toks = tokenize(title)
    # dedupe preserving order, drop single letters/digits
    seen, out = set(), []
    for w in toks:
        if len(w) < 2 or w.isdigit() or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 8:
            break
    return out


def classify(text: str, sessions, *, surface: str = "cli") -> dict:
    """Classify a message against known sessions.

    ``sessions`` is a list of dicts with at least ``title`` and
    ``keywords`` (list). Returns::

        {decision: "current"|"existing"|"new"|"none",
         session_id: str|None, title: str,
         score: float, confidence: float,
         latency_ms: float}

    The engine interprets ``current`` as the affinity hit. ``new`` means
    this is a fresh topic; ``none`` means the message is too generic to
    route (falls back to the current session without splitting).
    """
    t0 = time.perf_counter()
    msg_toks = tokenize(text)
    decision = "none"
    sid = None
    title = ""
    best_score = 0.0
    confidence = 0.0

    if _is_cmd_only(text):
        # slash-commands are never topics
        return {"decision": "none", "session_id": None, "title": "",
                "score": 0.0, "confidence": 0.0,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3)}

    if msg_toks:
        # real sessions first — a real hit always beats a dictionary hit
        best = None
        for s in sessions:
            sc = score(text, s.get("title", ""), s.get("keywords") or ())
            if sc > best_score:
                best_score = sc
                best = s
        if best is not None and best_score > 0:
            decision = "existing"
            sid = best.get("id")
            title = best.get("title", "")
            confidence = best_score
        else:
            # no real session matched → check the topic dictionary
            vocab_best = None
            vocab_score = 0.0
            for k, v in _load_dictionary().items():
                sc = score(text, k, v)
                if sc > vocab_score:
                    vocab_score = sc
                    vocab_best = k
            if vocab_best is not None and vocab_score > 0:
                # dictionary hit = brand-new topic candidate (not a real
                # session yet). +0.15: two keyword hits clear the engine's
                # default 0.6 threshold; a single keyword alone does not.
                best_score = vocab_score
                decision = "new"
                title = vocab_best
                confidence = min(1.0, vocab_score + 0.15)
    if decision == "none" and msg_toks and best_score == 0.0:
        # contentful message with zero overlap anywhere → genuinely new
        # topic (the engine's affinity bias still keeps it in-session)
        decision = "new"
        confidence = 0.0

    return {
        "decision": decision,
        "session_id": sid,
        "title": title,
        "score": best_score,
        "confidence": confidence,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
    }