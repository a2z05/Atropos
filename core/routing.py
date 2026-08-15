#!/usr/bin/env python3
"""Atropos task routing hub — which harness handles which task.

The hub maps *task categories* to harnesses. Unlike core/skills.py (which
routes skill *store* placement, hermes|claude only), this module is the
runtime dispatcher: given a free-text phrase it picks a category, then a
harness, and explains the decision.

Harnesses (canonical names):

  * ``clotho``   — the Hermes agent (comms, research, summaries)
  * ``lachesis`` — Claude Code (real coding, debugging, infra)
  * ``atropos``  — Atropos internal (self-maintenance: watch, backup, ...)
  * ``auto``     — not a harness; resolves by category default + heuristics

The routing map persists in ``~/.atropos/config.yaml`` under ``routing:``
via the settings schema (``routing.map``, ``routing.default``,
``routing.enabled``) — see core/settings.py.
"""
import re
from collections import OrderedDict

from . import settings

# The public ``set()`` function below shadows the builtin; capture it for
# internal isinstance()/set-construction use.
_SET_TYPE = set

# Canonical harness names. 'auto' is a meta-value: resolve it to a real
# harness via the category default table and keyword heuristics.
CORE_HARNESSES = ("clotho", "lachesis", "atropos", "auto")

# Aliases accepted in routing.map values and set(); normalized to canonical.
_HARNESS_ALIASES = {
    "clotho": "clotho",
    "hermes": "clotho",
    "lachesis": "lachesis",
    "claude": "lachesis",
    "atropos": "atropos",
    "internal": "atropos",
    "auto": "auto",
}

# Category → default harness. 'auto' defers to keyword heuristics.
DEFAULT_CATEGORIES = OrderedDict([
    ("coding", "lachesis"),      # code generation, refactoring
    ("debugging", "lachesis"),   # bugs, crashes, error traces
    ("devops", "lachesis"),      # deploy, docker, CI/CD, infra
    ("mlops", "lachesis"),       # training, inference, model mgmt
    ("research", "clotho"),      # web search, paper lookup
    ("summaries", "clotho"),     # log digest, TL;DR
    ("email", "clotho"),         # compose / triage mail
    ("media", "clotho"),         # images, audio, content
    ("monitoring", "atropos"),   # health, disk, uptime, alerts
    ("general", "auto"),         # anything else → heuristics
])

# Keyword tables for the auto heuristic. Kept as sets of lowercase tokens
# (and file extensions) — simple substring / token matching, no NLP.
_LACHESIS_KEYWORDS = {
    "fix", "debug", "refactor", "test", "compile", "build", "patch",
    "implement", "function", "class", "api", "syntax", "error",
    "python", "script", "code", "javascript", "typescript", "golang",
}
_LACHESIS_EXTS = {".py", ".js", ".ts", ".go", ".rs", ".java",
                  ".sh", ".yaml", ".yml", ".json", ".html", ".css"}
_CLOTHO_KEYWORDS = {
    "search", "summarize", "report", "analyze", "research", "find",
    "summarise", "digest", "lookup", "email",
}
_ATROPOS_KEYWORDS = {
    "system", "backup", "watch", "update", "disk", "health", "uptime",
    "alert", "monitor", "daemon",
}

# Word hints for category classification: a phrase token in this table
# votes for the category (used before the prefix scoring below).
_CATEGORY_WORDS = {
    "coding": ("code", "coding", "script", "python", "javascript", "typescript",
               "golang", "rust", "java", "function", "api", "refactor",
               "handler", "class"),
    "debugging": ("bug", "bugs", "crash", "fix", "error", "errors", "trace",
                  "stacktrace"),
    "devops": ("deploy", "deployment", "docker", "container", "ci", "cd",
               "infra", "server", "kubernetes"),
    "mlops": ("model", "training", "inference", "ml"),
    "research": ("research", "paper", "papers", "search", "literature"),
    "summaries": ("summarize", "summarise", "summary", "digest"),
    "email": ("email", "mail", "inbox"),
    "media": ("image", "images", "audio", "video", "photo"),
    "monitoring": ("monitor", "health", "disk", "uptime", "alert", "backup",
                   "watch", "system", "update"),
}

_CATEGORY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def categories() -> list:
    """Ordered category names: user overrides first, then the defaults.

    Categories the owner configured via ``routing.map`` that are not in
    ``DEFAULT_CATEGORIES`` (custom categories added with ``add()``) come
    first so the dashboard/CLI surface them prominently; the built-in
    defaults follow in their canonical order.
    """
    overrides = _map()
    custom = [c for c in overrides if c not in DEFAULT_CATEGORIES]
    return custom + list(DEFAULT_CATEGORIES.keys())


def normalize(harness: str) -> str:
    """Normalize a harness name/alias to its canonical form.

    Accepts ``clotho|hermes``, ``lachesis|claude``, ``atropos|internal``
    and ``auto`` (case-insensitive). Raises ValueError for anything else.
    """
    key = (harness or "").strip().lower()
    canon = _HARNESS_ALIASES.get(key)
    if canon is None:
        raise ValueError(
            f"unknown harness {harness!r} — expected one of: "
            + ", ".join(CORE_HARNESSES)
        )
    return canon


def _map() -> dict:
    """The persisted routing.map (category → harness name or alias)."""
    m = settings.get("routing.map", {})
    return dict(m) if isinstance(m, dict) else {}


def get(category: str, phrase: str = "") -> str:
    """Resolve the harness for a category (or phrase).

    Resolution order:

    1. explicit override in ``routing.map`` (normalized),
    2. the category's default in ``DEFAULT_CATEGORIES``,
    3. ``routing.default`` if that default is ``auto`` and no phrase is
       given, or
    4. keyword heuristics when the category resolves to ``auto`` and a
       phrase is provided.

    The returned value is always a canonical harness (never ``auto``).
    Unknown categories resolve through ``routing.default`` + heuristics,
    never raising.
    """
    override = _map().get(category)
    if override is not None:
        try:
            resolved = normalize(override)
        except ValueError:
            resolved = "auto"  # corrupt persisted value → heuristic path
        if resolved != "auto":
            return resolved
        if phrase:
            return _heuristic(phrase)
        return _fallback()
    default = DEFAULT_CATEGORIES.get(category, "auto")
    if default != "auto":
        return default
    if phrase:
        return _heuristic(phrase)
    return _fallback()


def _fallback() -> str:
    """The configured default harness (``routing.default``, validated)."""
    d = settings.get("routing.default", "auto")
    try:
        resolved = normalize(d)
    except ValueError:
        return "auto"
    return resolved if resolved != "auto" else "lachesis"


def _heuristic(phrase: str) -> str:
    """Score a phrase against the keyword tables; returns a harness name.

    ``auto`` only reaches this helper when an explicit override or the
    default resolved to it. Used by dispatch() as well.
    """
    score = _score(phrase)
    if score["lachesis"] >= score["clotho"] and score["lachesis"] >= score["atropos"]:
        return "lachesis"
    if score["clotho"] >= score["atropos"]:
        return "clotho"
    return "atropos"


def _score(phrase: str) -> dict:
    """Per-harness keyword score for a phrase. Lowercase token matching.

    * code extensions (.py, .js, ...) and code words → lachesis,
    * research/summarize words → clotho,
    * system-maintenance words → atropos.
    Each distinct keyword/ext counts once per harness.
    """
    s = (phrase or "").lower()
    tokens = re.findall(r"[a-z0-9_\.\-]+", s)
    lachesis = sum(1 for k in _LACHESIS_KEYWORDS if k in tokens)
    lachesis += sum(1 for ext in _LACHESIS_EXTS if ext in s)
    clotho = sum(1 for k in _CLOTHO_KEYWORDS if k in tokens)
    atropos = sum(1 for k in _ATROPOS_KEYWORDS if k in tokens)
    return {"lachesis": lachesis, "clotho": clotho, "atropos": atropos}


def dispatch(phrase: str) -> dict:
    """Route a free-text task phrase to a harness.

    Returns ``{category, harness, by, score}`` where ``by`` is one of:

    * ``override``  — matched ``routing.map``,
    * ``default``   — matched ``DEFAULT_CATEGORIES`` (or the configured
      ``routing.default``),
    * ``heuristic`` — keyword scoring decided it.

    ``score`` is the per-harness keyword score dict (zeros when a table
    entry decided). Unknown categories fall back to ``general``.
    """
    phrase = (phrase or "").strip()
    category = _classify(phrase)
    override = _map().get(category)
    if override is not None:
        try:
            resolved = normalize(override)
        except ValueError:
            resolved = "auto"
        if resolved != "auto":
            return {
                "category": category, "harness": resolved,
                "by": "override", "score": _score(phrase),
            }
        return {
            "category": category, "harness": _heuristic(phrase),
            "by": "heuristic", "score": _score(phrase),
        }
    default = DEFAULT_CATEGORIES.get(category, "auto")
    if default != "auto":
        return {
            "category": category, "harness": default,
            "by": "default", "score": _score(phrase),
        }
    if category == "general":
        # no default → configured routing.default or heuristics
        d = settings.get("routing.default", "auto")
        try:
            d = normalize(d)
        except ValueError:
            d = "auto"
        if d != "auto":
            return {
                "category": "general", "harness": d,
                "by": "default", "score": _score(phrase),
            }
    return {
        "category": category, "harness": _heuristic(phrase),
        "by": "heuristic", "score": _score(phrase),
    }


def _classify(phrase: str) -> str:
    """Pick the best-known category for a phrase via keyword matching.

    Returns a category name from ``DEFAULT_CATEGORIES``, or ``general``.
    Explicitly-named categories (e.g. a phrase containing the word
    "backup") win over keyword ties. Matched over both the raw phrase and
    its lowercase tokens, so "main.py" and "MAIN.PY" behave identically.
    """
    s = (phrase or "").lower()
    tokens = _SET_TYPE(re.findall(r"[a-z0-9_\-]+", s))
    # custom categories (added via add()/set()) match by exact token
    for name in _map():
        if name not in DEFAULT_CATEGORIES and name.lower() in tokens:
            return name
    best, best_score, best_count = "general", 0.0, 0
    for name, _def in DEFAULT_CATEGORIES.items():
        if name == "general":
            continue
        if name in tokens:
            # explicit category name beats everything
            return name
        # word-hint table votes (e.g. "fix" → debugging, "disk" → monitoring)
        hint = 0
        for tok in tokens:
            if tok in _CATEGORY_WORDS.get(name, ()):
                hint += 1
        # prefix scoring catches stems like "monitor" → monitoring
        score, count = _category_score(name, s, tokens)
        score += 0.5 * hint
        count += hint
        if count > 0 and (score > best_score
                          or (score == best_score and count > best_count)):
            best, best_score, best_count = name, score, count
    return best


def _category_score(name: str, s: str, tokens) -> tuple:
    """Keyword overlap of a category name with a phrase.

    Returns ``(score, count)``: score is 0.0..1.0 for partial-word matches
    (e.g. "monitor" → monitoring), 1.0 for exact token matches; count is
    the number of matched tokens. Matching is by prefix (phrase token
    starts with the category stem), which keeps "monitoring" and
    "monitor" mutually recognizable without a stemmer.
    """
    if name in tokens:
        return 1.0, 1
    total = 0.0
    count = 0
    for tok in tokens:
        if tok.startswith(name) and len(tok) > len(name):
            total += 0.8
            count += 1
        elif name.startswith(tok) and len(tok) >= 4:
            total += 0.6
            count += 1
    return total, count


def set(category: str, harness: str) -> dict:
    """Persist a routing override: category → harness.

    Validates the harness against the aliases (+ ``auto``) and the
    category against ``^[A-Za-z][A-Za-z0-9_-]{0,31}$``; raises ValueError
    on either. ``auto`` writes the meta-value (resolved at dispatch time).
    Returns the merged map.
    """
    canon = normalize(harness)
    if not _CATEGORY_RE.fullmatch(category):
        raise ValueError(
            f"invalid category {category!r} — must match "
            "[A-Za-z][A-Za-z0-9_-]{0,31}"
        )
    merged = _map()
    merged[category] = canon
    settings.set("routing.map", merged)
    return merged


def add(category: str, harness: str = "auto", **kwargs) -> dict:
    """Add a custom category (with optional default harness and keyword hints).

    ``harness`` defaults to ``auto`` (keyword heuristics). Extra kwargs
    (description, keywords...) are not persisted — they are accepted for
    API-compatibility with callers that pass metadata, and documented
    here so the hub can grow per-category heuristics later. Returns the
    merged map.
    """
    if not _CATEGORY_RE.fullmatch(category):
        raise ValueError(
            f"invalid category {category!r} — must match "
            "[A-Za-z][A-Za-z0-9_-]{0,31}"
        )
    merged = _map()
    merged[category] = normalize(harness)
    settings.set("routing.map", merged)
    return merged


def remove(category: str) -> bool:
    """Remove a routing override (and custom category). Returns True when removed."""
    merged = _map()
    if category not in merged:
        return False
    del merged[category]
    settings.set("routing.map", merged)
    return True


def list_config() -> dict:
    """Full routing configuration: map, default and enabled flag.

    Map values are normalized to canonical harness names (aliases and
    corrupt entries are cleaned up and re-persisted when changed).
    """
    raw = _map()
    cleaned = {}
    changed = False
    for cat, val in raw.items():
        try:
            cleaned[cat] = normalize(val)
        except ValueError:
            changed = True  # drop corrupt entries
        else:
            if cleaned[cat] != val:
                changed = True
    if changed:
        settings.set("routing.map", cleaned)
    return {
        "map": cleaned,
        "default": settings.get("routing.default", "auto"),
        "enabled": bool(settings.get("routing.enabled", True)),
    }
