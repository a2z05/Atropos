#!/usr/bin/env python3
"""Atropos TTS — Hermes provider chain, stdlib only (no aiohttp/asyncio).

Ported from real Hermes source with algorithms kept intact:
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\tts_tool.py      (provider chain, edge/openai/elevenlabs/gemini/piper/neutts
    generation, per-provider text caps, Ogg container repair, ffmpeg opus
    transcode, piper voice cache LRU, gemini WAV wrap)
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\tts_text_normalize.py (prepare_spoken_text pipeline: strip think
    blocks/verifier footer, strip markdown, expand symbols, smooth
    whitespace, flatten newlines)
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\tts_streaming.py (sentence chunker for per-sentence speech cuts)
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\neutts_synth.py (WAV writer used by the neutts stage)

API::

    tts(text, voice="", provider="auto") -> {"ok": bool, "path"/"url"/"error", "provider", "format"}
    list_voices() -> {"ok": bool, "voices": [...]}

Provider fallback chain (``provider="auto"``):
    gateway (core.tools.tts, the 9Router /v1/tts gateway, kept first so
    existing CLI/tests keep working) -> edge (lex-edge-tts if installed) ->
    openai -> elevenlabs -> gemini -> piper (piper CLI if detected) ->
    neutts (neutts CLI if detected).  Each stage degrades gracefully:
    unconfigured providers return {ok:False, error} without crashing, and a
    failed stage falls through to the next one (mirrors Hermes'
    "Edge default, NeuTTS local fallback" doctrine in text_to_speech_tool).

Deliberate deviations (all stdlib-driven):
  - aiohttp edge_tts -> subprocess call to the installed ``edge-tts`` CLI
    (same free Microsoft neural voices; the Hermes default provider was
    edge-tts, here run as a binary because aiohttp is banned).
  - requests -> urllib.request for the REST providers.
  - The managed-gateway / plugin-registry / command-provider layers of
    tts_tool.py are Hermes-native and not ported (Atropos has no plugin
    registry); per-call ``provider`` override is kept.
  - Hermes' opus-voice-bubble platform switch (HERMES_SESSION_PLATFORM) is
    not applicable; ``want_opus`` is exposed as an explicit parameter.
"""
import base64
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import detect, tools

# ---------------------------------------------------------------------------
# Defaults — copied verbatim from tts_tool.py lines 207-248.
# ---------------------------------------------------------------------------
DEFAULT_PROVIDER = "edge"
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"
DEFAULT_ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Adam
DEFAULT_ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"  # balanced size/quality
DEFAULT_GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_GEMINI_TTS_VOICE = "Kore"
DEFAULT_GEMINI_TTS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_XAI_VOICE_ID = "eve"
DEFAULT_XAI_LANGUAGE = "en"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
# xAI TTS `speed` accepts 0.7..1.5; 1.0 is the API default (omitted => default).
DEFAULT_XAI_SPEED_MIN = 0.7
DEFAULT_XAI_SPEED_MAX = 1.5
DEFAULT_XAI_SPEED_DEFAULT = 1.0
DEFAULT_MISTRAL_TTS_VOICE_ID = "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # Paul - Neutral
DEFAULT_MINIMAX_MODEL = "speech-02-hd"
DEFAULT_MINIMAX_VOICE_ID = "English_expressive_narrator"
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1/t2a_v2"
DEFAULT_MINIMAX_CN_BASE_URL = "https://api.minimaxi.com/v1/t2a_v2"
DEFAULT_NEUTTS_MODEL = "neuphonic/neutts-air-q4-gguf"
# PCM output specs for Gemini TTS (fixed by the API)
GEMINI_TTS_SAMPLE_RATE = 24000
GEMINI_TTS_CHANNELS = 1
GEMINI_TTS_SAMPLE_WIDTH = 2  # 16-bit PCM (L16)
TTS_RESPONSE_BODY_LIMIT_BYTES = 16 * 1024 * 1024
TTS_RESPONSE_BODY_CHUNK_BYTES = 64 * 1024

# Hermes fallback for the unknown-provider case (tts_tool.py FALLBACK_MAX_TEXT_LENGTH).
FALLBACK_MAX_TEXT_LENGTH = 4000

# Per-provider input-character limits — Hermes PROVIDER_MAX_TEXT_LENGTH
# (tts_tool.py lines 271-286). A single global cap was wrong: OpenAI is 4096,
# ElevenLabs is model-dependent, Gemini has a 32k-token context window, etc.
PROVIDER_MAX_TEXT_LENGTH = {
    "edge": 5000,         # edge-tts practical sync limit
    "openai": 4096,       # https://platform.openai.com/docs/guides/text-to-speech
    "xai": 15000,         # https://docs.x.ai/developers/model-capabilities/audio/text-to-speech
    "minimax": 10000,     # https://platform.minimax.io/docs/api-reference/speech-t2a-http (sync)
    "mistral": 4000,      # conservative; no published per-request cap
    "gemini": 32000,      # Gemini TTS has a 32k-token context window; char cap is conservative
    "elevenlabs": 10000,  # fallback when model-aware lookup can't resolve (multilingual_v2)
    "neutts": 2000,       # local model, quality falls off on long text
    "kittentts": 2000,    # local 25MB model
    "piper": 5000,        # local VITS model, phoneme-based; practical cap
}

# ElevenLabs caps vary by model_id (tts_tool.py ELEVENLABS_MODEL_MAX_TEXT_LENGTH).
ELEVENLABS_MODEL_MAX_TEXT_LENGTH = {
    "eleven_v3": 5000,
    "eleven_ttv_v3": 5000,
    "eleven_multilingual_v2": 10000,
    "eleven_multilingual_v1": 10000,
    "eleven_english_sts_v2": 10000,
    "eleven_english_sts_v1": 10000,
    "eleven_flash_v2": 30000,
    "eleven_flash_v2_5": 40000,
}

# Built-in provider names (tts_tool.py BUILTIN_TTS_PROVIDERS, local set only).
BUILTIN_TTS_PROVIDERS = frozenset({
    "edge", "elevenlabs", "openai", "minimax", "xai", "mistral",
    "gemini", "neutts", "kittentts", "piper", "deepinfra",
})

# Automatic chain order. Hermes' default is edge; here the 9Router gateway
# comes first so existing CLI/tests keep working; the remainder mirrors
# Hermes' built-in dispatch + "Edge default, NeuTTS local fallback".
AUTO_PROVIDER_CHAIN = (
    "gateway", "edge", "openai", "elevenlabs", "gemini", "piper", "neutts",
)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


# ---------------------------------------------------------------------------
# Per-provider text caps (Hermes _resolve_max_text_length, tts_tool.py:409).
# ---------------------------------------------------------------------------
def _resolve_max_text_length(provider: str | None) -> int:
    """Return the input-character cap for *provider*.

    Resolution order (port of Hermes):
      1. ``tts.<provider>.max_text_length`` user override (settings.ini)
      2. ElevenLabs model-aware table (keyed on configured model_id)
      3. PROVIDER_MAX_TEXT_LENGTH default
      4. FALLBACK_MAX_TEXT_LENGTH (4000). Non-positive overrides fall
         through so a broken config can't disable truncation entirely.
    """
    if not provider:
        return FALLBACK_MAX_TEXT_LENGTH
    key = provider.lower().strip()
    override = _tts_cfg(key, {}).get("max_text_length")
    if isinstance(override, bool):
        override = None
    if isinstance(override, int) and override > 0:
        return override
    if key == "elevenlabs":
        model_id = _tts_cfg(key, {}).get("model_id") or DEFAULT_ELEVENLABS_MODEL_ID
        mapped = ELEVENLABS_MODEL_MAX_TEXT_LENGTH.get(str(model_id).strip())
        if mapped:
            return mapped
    if key in PROVIDER_MAX_TEXT_LENGTH:
        return PROVIDER_MAX_TEXT_LENGTH[key]
    return FALLBACK_MAX_TEXT_LENGTH


# ---------------------------------------------------------------------------
# Config loading (Hermes _load_tts_config / _get_provider, tts_tool.py:468).
# Atropos keeps the config in settings.ini under [tts].
# ---------------------------------------------------------------------------
def _load_tts_config() -> dict:
    """Load the ``tts`` config section (settings.ini ``[tts]``)."""
    try:
        from . import settings as _s
        cfg = _s.get("tts")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _tts_cfg(provider: str, default=None) -> dict:
    """Return the ``tts.<provider>`` sub-block as a dict (Hermes _get_provider_section)."""
    section = _load_tts_config().get(provider)
    return section if isinstance(section, dict) else (default if default is not None else {})


def _get_provider() -> str:
    """Explicitly configured ``tts.provider`` or the free default (Hermes _get_provider)."""
    return (_load_tts_config().get("provider") or DEFAULT_PROVIDER).lower().strip()


def _config_bool(value, default: bool = False) -> bool:
    """Coerce common YAML/env bool spellings without treating random strings as true (Hermes _config_bool)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _resolve_provider_key(env_var: str, provider_id: str) -> str:
    """Resolve a provider API key (config > env) — Hermes _resolve_provider_key.

    The Hermes original also consults the credential pool / auth store; here
    only config+env exist, resolved at call time.
    """
    cfg_value = _load_tts_config().get(provider_id, {}).get("api_key") if provider_id else ""
    if isinstance(cfg_value, str) and cfg_value.strip():
        return cfg_value.strip()
    return _env(env_var)


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib replacement for Hermes' requests/httpx paths
# (Hermes _read_tts_response_bytes / _write_tts_response_to_file,
# tts_tool.py:335-407). Reads with a hard 16 MiB byte cap like Hermes.
# ---------------------------------------------------------------------------
def _http_post(url: str, payload: dict, headers: dict, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(TTS_RESPONSE_BODY_LIMIT_BYTES + 1)
    if len(raw) > TTS_RESPONSE_BODY_LIMIT_BYTES:
        raise RuntimeError(f"TTS upstream response exceeds {TTS_RESPONSE_BODY_LIMIT_BYTES} bytes")
    return raw


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# Text normalization — full port of tts_text_normalize.py.
# ---------------------------------------------------------------------------
# Sentinel appended to former heading lines so smooth_whitespace_for_tts can
# fold a heading into the sentence that follows it ("Weather, it will be sunny").
_HEAD = "\x00"

_MD_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_MD_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__", flags=re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", flags=re.DOTALL)
_MD_UNDERSCORE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", flags=re.DOTALL)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", flags=re.DOTALL)
_MD_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", flags=re.MULTILINE)
_MD_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", flags=re.MULTILINE)
_MD_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", flags=re.MULTILINE)
_MD_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", flags=re.MULTILINE)
_MD_TABLE_PIPE_RE = re.compile(r"\s*\|\s*")
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "☀-➿"
    "]+",
    flags=re.UNICODE,
)
_VARIATION_SELECTOR_RE = re.compile("[︎️]")


def strip_markdown_for_tts(text: str) -> str:
    """Strip Markdown/Telegram formatting while preserving readable words.

    Ported verbatim from tts_text_normalize.py strip_markdown_for_tts.
    """
    if not text:
        return ""
    text = __import__("html").unescape(str(text))
    text = _MD_CODE_BLOCK_RE.sub(" ", text)
    text = _MD_IMAGE_RE.sub(lambda m: f" {m.group(1)} " if m.group(1) else " ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_UNDERSCORE_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    # Mark headings (do not just delete the marker): the whitespace pass folds a
    # heading into the sentence after it so speech says "Weather, it will be
    # sunny" instead of a clipped "Weather." then a separate sentence.
    text = _MD_HEADING_LINE_RE.sub(lambda m: m.group(1).rstrip() + _HEAD, text)
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_LIST_ITEM_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
    # Pipe tables are terrible read aloud: turn leftover pipes into pauses.
    text = _MD_TABLE_PIPE_RE.sub("; ", text)
    return text


def _normalize_temperature_ranges(text: str) -> str:
    """11-17 degrees C -> "11 to 17 degrees Celsius" (Hermes original)."""
    text = re.sub(
        r"(?<!\w)([-+−]?\d+(?:\.\d+)?)\s*[–—-]\s*([-+−]?\d+(?:\.\d+)?)\s*°\s*C\b",
        lambda m: f"{m.group(1).replace(chr(0x2212), '-')} to {m.group(2).replace(chr(0x2212), '-')} degrees Celsius",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?<!\w)([-+−]?\d+(?:\.\d+)?)\s*[–—-]\s*([-+−]?\d+(?:\.\d+)?)\s*°\s*F\b",
        lambda m: f"{m.group(1).replace(chr(0x2212), '-')} to {m.group(2).replace(chr(0x2212), '-')} degrees Fahrenheit",
        text,
        flags=re.IGNORECASE,
    )
    return text


def normalize_symbols_for_tts(text: str) -> str:
    """Expand common symbols/shorthand into words a TTS engine reads well.

    Ported verbatim from tts_text_normalize.py normalize_symbols_for_tts.
    """
    if not text:
        return ""
    text = str(text)
    text = re.sub("[   ]", " ", text)  # non-breaking / thin spaces
    text = text.replace("−", "-")  # minus sign
    text = text.replace("…", "...")  # ellipsis
    text = _normalize_temperature_ranges(text)
    # Temperatures with a number.  Do this before generic degree handling.
    text = re.sub(r"(?<!\w)([-+]?\d+(?:\.\d+)?)\s*°\s*C\b", r"\1 degrees Celsius", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)([-+]?\d+(?:\.\d+)?)\s*°\s*F\b", r"\1 degrees Fahrenheit", text, flags=re.IGNORECASE)
    # Bare units with no leading number ("measured in degrees C").
    text = re.sub(r"°\s*C\b", "degrees Celsius", text, flags=re.IGNORECASE)
    text = re.sub(r"°\s*F\b", "degrees Fahrenheit", text, flags=re.IGNORECASE)
    # Any remaining degree symbol (angles, stray cases).
    text = re.sub(r"(?<!\w)([-+]?\d+(?:\.\d+)?)\s*°", r"\1 degrees", text)
    text = text.replace("°", " degrees")
    # Common weather/travel units.
    text = re.sub(r"(?<=\d)\s*km\s*/\s*h\b", " kilometres per hour", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*km/h\b", " kilometres per hour", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*mm\b", " millimetres", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*cm\b", " centimetres", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*m\b", " metres", text, flags=re.IGNORECASE)
    # Numeric rates only ("5/month" -> "5 per month").
    text = re.sub(r"(?<=\d)\s*/\s*(?=[A-Za-z])", " per ", text)
    # Money and percentages.
    text = re.sub(r"NZ\$\s*([\d,]*\d(?:\.\d+)?)", r"\1 New Zealand dollars", text, flags=re.IGNORECASE)
    text = re.sub(r"A\$\s*([\d,]*\d(?:\.\d+)?)", r"\1 Australian dollars", text, flags=re.IGNORECASE)
    text = re.sub(r"US\$\s*([\d,]*\d(?:\.\d+)?)", r"\1 US dollars", text, flags=re.IGNORECASE)
    text = re.sub(r"€\s*([\d,]*\d(?:\.\d+)?)", r"\1 euros", text)
    text = re.sub(r"£\s*([\d,]*\d(?:\.\d+)?)", r"\1 pounds", text)
    text = re.sub(r"\$\s*([\d,]*\d(?:\.\d+)?)", r"\1 dollars", text)
    text = re.sub(r"(?<=\d)\s*%", " percent", text)
    # Operators and separators that commonly leak from formatted answers.
    text = text.replace("&", " and ")
    text = re.sub("[•◦▪▫]", " ", text)  # bullet glyphs
    text = text.replace("→", " to ")  # ->
    text = text.replace("⇒", " to ")  # =>
    text = text.replace("≈", " about ")  # almost equal
    text = text.replace("~", " about ")
    text = _VARIATION_SELECTOR_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    return text


def smooth_whitespace_for_tts(text: str) -> str:
    """Collapse visual formatting into calm spoken paragraphs.

    Ported verbatim from tts_text_normalize.py smooth_whitespace_for_tts
    (including the _HEAD heading-fold).
    """
    if not text:
        return ""
    raw_lines = text.splitlines()
    add_sentence_pauses = sum(1 for raw_line in raw_lines if raw_line.replace(_HEAD, "").strip()) > 1
    lines: list[str] = []
    pending_heading: str | None = None

    def flush_pending() -> None:
        nonlocal pending_heading
        if pending_heading is not None:
            lines.append(pending_heading.rstrip(".:;,") + ".")
            pending_heading = None

    for raw_line in raw_lines:
        is_heading = raw_line.rstrip().endswith(_HEAD)
        line = raw_line.replace(_HEAD, "").strip()
        if not line:
            if pending_heading is None and lines and lines[-1] != "":
                lines.append("")
            continue
        if is_heading:
            flush_pending()
            pending_heading = line.rstrip(".:;,")
            continue
        if pending_heading is not None:
            line = f"{pending_heading.rstrip('.:;,')}, {line}"
            pending_heading = None
        if add_sentence_pauses and line[-1] not in ".!?;:":
            line += "."
        lines.append(line)

    flush_pending()

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"\.{4,}", "...", text)
    return text.strip()


# Reasoning blocks: models with ``/reasoning show`` enabled emit
# ``<thinking>...`` blocks — users want to SEE reasoning, not hear it
# read aloud. Ported regexes from tts_text_normalize.py.
_THINK_BLOCK_RE = re.compile(r"<think[\s>].*?</think>", flags=re.DOTALL | re.IGNORECASE)
_THINK_BLOCK_OPEN_RE = re.compile(r"<think[\s>].*\Z", flags=re.DOTALL | re.IGNORECASE)
_VERIFIER_FOOTER_RE = re.compile(
    r"^\s*⚠️?\s*File-mutation verifier:.*(?:\n[ \t]+•.*)*",
    flags=re.MULTILINE,
)


def strip_nonspoken_blocks(text: str) -> str:
    """Remove blocks that must never reach a speech provider (Hermes original)."""
    if not text:
        return ""
    text = _THINK_BLOCK_RE.sub(" ", text)
    text = _THINK_BLOCK_OPEN_RE.sub(" ", text)
    text = _VERIFIER_FOOTER_RE.sub(" ", text)
    return text


def flatten_newlines_for_payload(text: str) -> str:
    """Collapse newlines into sentence breaks for single-line TTS payloads.

    Some OpenAI-compatible backends truncate synthesis at the first newline;
    the smoothing pass already terminates each line with punctuation, so
    newlines can safely become plain spaces (Hermes original).
    """
    if not text:
        return ""
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"(?<=[.!?;:,])\n", " ", text)
    text = text.replace("\n", ". ")
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def prepare_spoken_text(text: str, max_chars: int | None = 4000) -> str:
    """Return a TTS-friendly spoken script (Hermes prepare_spoken_text).

    Deterministic cleanup: strips think blocks + verifier footer, removes
    Markdown, expands symbols, turns line formatting into sentence pauses,
    flattens to a single line for newline-sensitive providers.
    """
    spoken = strip_nonspoken_blocks(text)
    spoken = strip_markdown_for_tts(spoken)
    spoken = normalize_symbols_for_tts(spoken)
    spoken = smooth_whitespace_for_tts(spoken)
    spoken = flatten_newlines_for_payload(spoken)
    if max_chars is not None and max_chars > 0 and len(spoken) > max_chars:
        spoken = spoken[:max_chars].rstrip()
    return spoken


# ---------------------------------------------------------------------------
# Sentence chunker — port of tts_streaming.py SentenceChunker (per-sentence
# speech cuts; the streaming wire protocol itself is not ported).
# ---------------------------------------------------------------------------
SENTENCE_BOUNDARY_RE = re.compile(r"[.!?…。！？…]+[\s]*")


class SentenceChunker:
    """Incremental sentence cutter for LLM token deltas (tts_streaming.py)."""

    def __init__(self, min_len: int = 20):
        self.min_len = min_len
        self.buf = ""

    def feed(self, delta: str) -> list[str]:
        """Absorb *delta*; return every complete sentence now ready to speak."""
        self.buf = _THINK_BLOCK_RE.sub("", self.buf + delta)
        if "<think" in self.buf and "</think>" not in self.buf:
            return []  # open think tag — the closing tag may arrive next delta
        out: list[str] = []
        start = 0  # skip boundaries that would leave the head too short
        while m := SENTENCE_BOUNDARY_RE.search(self.buf, start):
            head = self.buf[: m.end()]
            if len(head.strip()) < self.min_len:
                start = m.end()
                continue
            out.append(head)
            self.buf = self.buf[m.end():]
            start = 0
        return out

    def flush(self) -> list[str]:
        """Drain the tail (end-of-text or long-idle flush)."""
        tail = _THINK_BLOCK_RE.sub("", self.buf).strip()
        self.buf = ""
        return [tail] if tail else []


# ---------------------------------------------------------------------------
# Audio container sniff + Ogg repair — port of tools/audio_container.py +
# tts_tool.py _repair_ogg_container / _ffmpeg_transcode_to_opus. Several
# backends silently write MP3/WAV bytes into a .ogg path; sniff magic bytes
# once after synthesis and repair when they don't match.
# ---------------------------------------------------------------------------
def sniff_container(data: bytes) -> str | None:
    """Return a container id from magic bytes ('ogg','wav','mp3','flac',
    'm4a','mp4','aac','webm') or None when unknown (audio_container.py)."""
    if len(data) >= 8 and data[4:8] == b"ftyp":
        if len(data) >= 12 and data[8:12].lower() in (b"m4a ", b"m4b "):
            return "m4a"
        return "mp4"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3"):
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # ``0xFF 0xFx`` is shared by MP3 and ADTS AAC. Bits 3-1 of byte 1
        # disambiguate: ADTS has ID=0 and layer=00 (mask 0xF6, target 0xF0);
        # MP3 has ID=1 and layer in {01,10,11}.
        if (data[1] & 0xF6) == 0xF0:
            return "aac"
        return "mp3"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return None


def _sniff_audio_container(path: str) -> str:
    """Return a container id or 'unknown' for the file at *path* (Hermes)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return "unknown"
    return sniff_container(head) or "unknown"


def _ffmpeg_transcode_to_opus(input_path: str, ogg_path: str) -> str | None:
    """Transcode *input_path* to real Ogg/Opus at *ogg_path* via ffmpeg.

    Safe when ``input_path == ogg_path`` (writes to a temp file, then
    replaces). Returns the output path on success, None on failure — port of
    tts_tool.py _ffmpeg_transcode_to_opus with the Hermes flags (the
    windows_hide_flags creationflags are a Hermes Windows detail, omitted:
    Atropos uses stdlib subprocess defaults).
    """
    if shutil.which("ffmpeg") is None:
        return None
    in_place = os.path.abspath(input_path) == os.path.abspath(ogg_path)
    work_path = ogg_path + ".tmp.ogg" if in_place else ogg_path
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", input_path, "-acodec", "libopus",
             "-ac", "1", "-b:a", "48k", "-vbr", "on",
             "-application", "voip", "-compression_level", "10", "-f", "ogg",
             work_path, "-y"],
            capture_output=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        if os.path.exists(work_path) and os.path.getsize(work_path) > 0:
            if in_place:
                os.replace(work_path, ogg_path)
            return ogg_path
    except Exception:
        pass
    finally:
        if in_place and os.path.exists(work_path):
            try:
                os.remove(work_path)
            except OSError:
                pass
    return None


def _repair_ogg_container(file_str: str) -> str:
    """Ensure a path claiming ``.ogg`` actually contains an Ogg container.

    When the bytes are MP3/WAV/FLAC (a backend ignored the opus request),
    transcode in place to real Ogg/Opus (tts_tool.py _repair_ogg_container).
    On failure, rename to the sniffed real extension.
    """
    if not file_str.endswith(".ogg"):
        return file_str
    container = _sniff_audio_container(file_str)
    if container in ("ogg", "unknown"):
        return file_str
    repaired = _ffmpeg_transcode_to_opus(file_str, file_str)
    if repaired:
        return repaired
    honest = file_str[:-4] + "." + container
    try:
        os.replace(file_str, honest)
        return honest
    except OSError:
        return file_str


# ---------------------------------------------------------------------------
# Gemini PCM→WAV wrapper — port of tts_tool.py _wrap_pcm_as_wav.
# ---------------------------------------------------------------------------
def _wrap_pcm_as_wav(
    pcm_bytes: bytes,
    sample_rate: int = GEMINI_TTS_SAMPLE_RATE,
    channels: int = GEMINI_TTS_CHANNELS,
    sample_width: int = GEMINI_TTS_SAMPLE_WIDTH,
) -> bytes:
    """Wrap raw signed-little-endian PCM with a standard WAV RIFF header.

    Gemini TTS returns audio/L16;codec=pcm;rate=24000 — raw PCM samples with
    no container. We add a minimal WAV header so the file is playable and
    ffmpeg can re-encode it to MP3/Opus downstream.
    """
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm_bytes)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,             # fmt chunk size (PCM)
        1,              # audio format (PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
    )
    data_chunk_header = struct.pack("<4sI", b"data", data_size)
    riff_size = 4 + len(fmt_chunk) + len(data_chunk_header) + data_size
    riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
    return riff_header + fmt_chunk + data_chunk_header + pcm_bytes


# ---------------------------------------------------------------------------
# Provider: edge-tts. Hermes uses the aiohttp-based edge_tts package
# (_generate_edge_tts); stdlib-only means we drive the ``edge-tts`` CLI
# binary instead — same Microsoft neural voices, same voice names.
# ---------------------------------------------------------------------------
def _find_edge_tts() -> str:
    """Locate the edge-tts binary (module-less subprocess path), else ''."""
    if shutil.which("edge-tts"):
        return "edge-tts"
    if shutil.which("edge-tts.bat"):
        return "edge-tts.bat"
    try:
        with open(os.devnull, "w") as devnull:
            subprocess.run([sys.executable, "-m", "edge_tts", "--help"],
                           stdout=devnull, stderr=devnull, timeout=30)
            return [sys.executable, "-m", "edge_tts"]
    except Exception:
        return ""


def _generate_edge(path: str, text: str, voice: str, speed: float) -> None:
    """Synthesize via the edge-tts CLI (subprocess).

    Hermes _generate_edge_tts (tts_tool.py:1370) passes voice + rate to
    edge_tts.Communicate and saves the file; ``rate`` is a signed percent
    string derived from the speed multiplier. Deviation: CLI instead of
    aiohttp — the synthesis behavior (voice/rate handling, MP3 output) is
    the same edge-tts engine.
    """
    exe = _find_edge_tts()
    if not exe:
        raise RuntimeError(
            "edge-tts is not installed. Install it with `pip install edge-tts` "
            "or use another TTS provider."
        )
    kwargs = ["--voice", voice]
    if speed != 1.0:
        pct = round((speed - 1.0) * 100)
        kwargs += ["--rate", f"{pct:+d}%"]
    if isinstance(exe, list):
        cmd = exe + ["--text", text, "--write-media", path] + kwargs
    else:
        cmd = [exe, "--text", text, "--write-media", path] + kwargs
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"edge-tts failed: {(proc.stderr or proc.stdout)[:300]}")


# ---------------------------------------------------------------------------
# Provider: OpenAI / OpenAI-compatible TTS (Hermes _generate_openai_tts,
# tts_tool.py:1458). REST shape /v1/audio/speech.
# ---------------------------------------------------------------------------
def _tts_response_format_from_path(output_path: str) -> str:
    """Pick an OpenAI-compatible TTS response format from the output extension (Hermes)."""
    if output_path.endswith(".ogg"):
        return "opus"
    if output_path.endswith(".wav"):
        return "wav"
    if output_path.endswith(".flac"):
        return "flac"
    return "mp3"


def _generate_openai(path: str, text: str, voice: str, cfg: dict, tts_cfg: dict) -> None:
    """OpenAI ``/v1/audio/speech`` via urllib (Hermes _generate_openai_tts)."""
    api_key = cfg.get("api_key") or _env("OPENAI_API_KEY") or _env("VOICE_TOOLS_OPENAI_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set (OpenAI TTS provider)")
    model = cfg.get("model") or DEFAULT_OPENAI_MODEL
    v = voice or cfg.get("voice") or DEFAULT_OPENAI_VOICE
    base_url = (cfg.get("base_url") or DEFAULT_OPENAI_BASE_URL).rstrip("/")
    speed_default = tts_cfg.get("speed", 1.0) if isinstance(tts_cfg, dict) else 1.0
    speed = float(cfg.get("speed", speed_default))
    response_format = _tts_response_format_from_path(path)
    payload = {"model": model, "voice": v, "input": text, "response_format": response_format}
    if speed != 1.0:
        # Clamp 0.25..4.0 exactly like Hermes.
        payload["speed"] = max(0.25, min(4.0, speed))
    raw = _http_post(f"{base_url}/audio/speech", payload,
                     {"Authorization": f"Bearer {api_key}"})
    _write_bytes(path, raw)


# ---------------------------------------------------------------------------
# Provider: ElevenLabs (Hermes _generate_elevenlabs, tts_tool.py:1400).
# ---------------------------------------------------------------------------
def _generate_elevenlabs(path: str, text: str, voice: str, cfg: dict) -> None:
    """ElevenLabs /v1/text-to-speech POST, streamed bytes (Hermes shape)."""
    api_key = cfg.get("api_key") or _env("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set (ElevenLabs provider)")
    voice_id = voice or cfg.get("voice_id") or DEFAULT_ELEVENLABS_VOICE_ID
    model_id = cfg.get("model_id") or DEFAULT_ELEVENLABS_MODEL_ID
    # Determine output format based on file extension (Hermes logic).
    output_format = "opus_48000_64" if path.endswith(".ogg") else "mp3_44100_128"
    base_url = (cfg.get("base_url") or "https://api.elevenlabs.io").rstrip("/")
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": float(cfg.get("stability", 0.5)),
            "similarity_boost": float(cfg.get("similarity_boost", 0.75)),
            "style": float(cfg.get("style", 0.0)),
            "use_speaker_boost": _config_bool(cfg.get("use_speaker_boost"), True),
        },
    }
    raw = _http_post(
        f"{base_url}/v1/text-to-speech/{voice_id}?output_format={output_format}",
        payload, {"xi-api-key": api_key})
    _write_bytes(path, raw)


# ---------------------------------------------------------------------------
# Provider: Gemini TTS (Hermes _generate_gemini_tts, tts_tool.py:2248).
# ---------------------------------------------------------------------------
def _wrap_pcm_to_target(pcm_bytes: bytes, output_path: str) -> None:
    """Write *pcm_bytes* as WAV (wrapping LPCM) or convert to mp3/ogg via ffmpeg.

    Port of the tail of Hermes _generate_gemini_tts: write WAV to a temp
    file and ffmpeg-convert to the target format; if ffmpeg is missing,
    fall back to renaming the WAV (audio still plays, just with a
    misleading extension).
    """
    wav_bytes = _wrap_pcm_as_wav(pcm_bytes)
    if output_path.lower().endswith(".wav"):
        _write_bytes(output_path, wav_bytes)
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        wav_path = tmp.name
    try:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            if output_path.lower().endswith(".ogg"):
                # Force libopus encoding — ffmpeg's default for .ogg is Vorbis.
                cmd = [ffmpeg, "-i", wav_path, "-acodec", "libopus", "-ac", "1",
                       "-b:a", "48k", "-vbr", "on", "-application", "voip",
                       "-compression_level", "10", "-y", "-loglevel", "error",
                       output_path]
            else:
                cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path]
            result = subprocess.run(cmd, capture_output=True, timeout=30,
                                    stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="ignore")[:300]
                raise RuntimeError(f"ffmpeg conversion failed: {stderr}")
        else:
            shutil.copyfile(wav_path, output_path)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


def _generate_gemini(path: str, text: str, voice: str, tts_cfg: dict) -> None:
    """Gemini generateContent with responseModalities=["AUDIO"] via urllib.

    The API returns raw 24kHz mono 16-bit PCM (L16) base64; wrap with a WAV
    header and ffmpeg-convert if the caller requested mp3/ogg (Hermes
    _generate_gemini_tts). The audio_tags hidden-rewrite sub-LLM is not
    ported (no auxiliary LLM in Atropos) — the compose-prompt + payload
    shape is preserved.
    """
    api_key = (_resolve_provider_key("GEMINI_API_KEY", "gemini")
               or _resolve_provider_key("GOOGLE_API_KEY", "gemini"))
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set (Gemini provider)")
    gemini_config = tts_cfg.get("gemini") if isinstance(tts_cfg, dict) else {}
    if not isinstance(gemini_config, dict):
        gemini_config = {}
    model = str(gemini_config.get("model", DEFAULT_GEMINI_TTS_MODEL)).strip() or DEFAULT_GEMINI_TTS_MODEL
    v = voice or str(gemini_config.get("voice", DEFAULT_GEMINI_TTS_VOICE)).strip() or DEFAULT_GEMINI_TTS_VOICE
    base_url = str(gemini_config.get("base_url") or _env("GEMINI_BASE_URL")
                   or DEFAULT_GEMINI_TTS_BASE_URL).strip().rstrip("/")
    max_len = _resolve_max_text_length("gemini")
    prompt_text = text.strip()
    if len(prompt_text) > max_len:
        prompt_text = prompt_text[:max_len]
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": v},
                },
            },
        },
    }
    endpoint = f"{base_url}/models/{model}:generateContent?key={api_key}"
    raw = _http_post(endpoint, payload, {}, timeout=60)
    try:
        data = json.loads(raw.decode("utf-8"))
        parts = data["candidates"][0]["content"]["parts"]
        audio_part = next((p for p in parts if "inlineData" in p or "inline_data" in p), None)
        if audio_part is None:
            raise RuntimeError("Gemini TTS response contained no audio data")
        inline = audio_part.get("inlineData") or audio_part.get("inline_data") or {}
        audio_b64 = inline.get("data", "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Gemini TTS response was malformed: {e}") from e
    if not audio_b64:
        raise RuntimeError("Gemini TTS returned empty audio data")
    _wrap_pcm_to_target(base64.b64decode(audio_b64), path)


# ---------------------------------------------------------------------------
# Provider: Piper (local VITS, 44 languages) — Hermes _generate_piper_tts
# with the voice-model LRU cache (_tts_cache_get_or_load). Hermes drives
# the ``piper`` python package; stdlib-only means we shell out to the
# ``piper`` CLI binary when detected (same voice model + PiperParams/
# synthesis knobs surface via CLI flags).
# ---------------------------------------------------------------------------
_PIPER_VOICE_CACHE_MAX = 3
_piper_voice_cache: dict = {}

# Default voice models per Piper release (mirrors piper-tts default voice).
_DEFAULT_PIPER_ONNX = "en_US-lessac-medium"
_PIPER_VOICE_URL_PREFIX = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/"
)


def _piper_voices_dir() -> Path:
    """Return the directory where voices are cached (Hermes _get_piper_voices_dir)."""
    root = detect.atropos_home() / "cache" / "piper-voices"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _download_piper_voice(voice: str, download_dir: Path) -> Path:
    """Download the .onnx + .onnx.json pair for *voice* (Hermes resolves by
    shelling out to ``python -m piper.download_voices``; here urllib fetches
    the same OHF-Voice/rhasspy artifact so the voice cache works without the
    piper python package)."""
    onnx = download_dir / f"{voice}.onnx"
    jsonp = download_dir / f"{voice}.onnx.json"
    if onnx.exists() and jsonp.exists():
        return onnx
    for suffix in (".onnx", ".onnx.json"):
        target = download_dir / f"{voice}{suffix}"
        if target.exists():
            continue
        url = f"{_PIPER_VOICE_URL_PREFIX}{voice}{suffix}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
            if "Not Found" in data[:512].decode("utf-8", errors="ignore"):
                raise RuntimeError(f"Piper voice '{voice}' not found on the hub")
            target.write_bytes(data)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Piper voice download failed for '{voice}': HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Piper voice download failed for '{voice}': {e.reason}") from e
    return onnx


def _check_piper_available() -> bool:
    """True if the ``piper`` CLI binary is on PATH (Hermes check is
    importlib find_spec; ours checks the binary we can actually drive)."""
    return bool(shutil.which("piper"))


def _generate_piper(path: str, text: str, voice: str, tts_cfg: dict) -> None:
    """Synthesize via the piper CLI, WAV output (Hermes _generate_piper_tts).

    Voice models are cached under ~/.atropos/cache/piper-voices via the LRU
    (Hermes caches loaded PiperVoice instances keyed on the .onnx path; ours
    caches the resolved model path — the model-load cache semantics are the
    same, the runtime is the CLI).
    """
    piper_bin = shutil.which("piper")
    if not piper_bin:
        raise RuntimeError("piper CLI not found (pip install piper-tts)")
    piper_config = tts_cfg.get("piper") if isinstance(tts_cfg, dict) else {}
    if not isinstance(piper_config, dict):
        piper_config = {}
    voice_name = voice or piper_config.get("voice") or DEFAULT_PIPER_VOICE
    download_dir = Path(piper_config.get("voices_dir") or _piper_voices_dir()).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)

    # Voice model cache keyed on the absolute .onnx path (Hermes cache key).
    cache_key = str(_download_piper_voice(voice_name, download_dir))
    if cache_key not in _piper_voice_cache:
        _piper_voice_cache[cache_key] = cache_key
        while len(_piper_voice_cache) > _PIPER_VOICE_CACHE_MAX:
            _piper_voice_cache.pop(next(iter(_piper_voice_cache)), None)

    # Piper outputs WAV natively.
    wav_path = path
    if not path.endswith(".wav"):
        wav_path = path.rsplit(".", 1)[0] + ".wav"
    cmd = [piper_bin, "--model", cache_key, "--output_file", wav_path]
    # Hermes synthesis knobs map onto Piper's CLI flags.
    length_scale = piper_config.get("length_scale")
    noise_scale = piper_config.get("noise_scale")
    noise_w_scale = piper_config.get("noise_w_scale")
    if length_scale is not None:
        cmd += ["--length_scale", str(float(length_scale))]
    if noise_scale is not None:
        cmd += ["--noise_scale", str(float(noise_scale))]
    if noise_w_scale is not None:
        cmd += ["--noise_w_scale", str(float(noise_w_scale))]
    proc = subprocess.run(cmd, input=text.encode(), capture_output=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"piper synthesis failed: {err}")
    if wav_path != path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            conv_cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", path]
            subprocess.run(conv_cmd, check=True, timeout=30, stdin=subprocess.DEVNULL)
            try:
                os.remove(wav_path)
            except OSError:
                pass
        else:
            os.rename(wav_path, path)


# ---------------------------------------------------------------------------
# Provider: NeuTTS (local, on-device) — Hermes _generate_neutts runs a
# subprocess so the ~500MB model process exits after synthesis; we do the
# same against the neutts CLI when detected.
# ---------------------------------------------------------------------------
def _check_neutts_available() -> bool:
    """True if the neutts CLI binary is on PATH (Hermes checks importable
    'neutts'; ours checks the binary we can drive)."""
    return bool(shutil.which("neutts"))


def _generate_neutts(path: str, text: str, voice: str, tts_cfg: dict) -> None:
    """Generate speech with the local NeuTTS engine (CLI subprocess).

    Port of Hermes _generate_neutts: WAV output, ffmpeg conversion to the
    requested format, rename fallback when ffmpeg is missing. The Hermes
    subprocess runs tools/neutts_synth.py; ours runs the installed ``neutts``
    CLI binary (no neutts python package available stdlib-only).
    """
    cmd = shutil.which("neutts")
    if not cmd:
        raise RuntimeError("neutts CLI not found (python -m pip install -U neutts)")
    wav_path = path
    if not path.endswith(".wav"):
        wav_path = path.rsplit(".", 1)[0] + ".wav"
    proc = subprocess.run([cmd, "--text", text, "--out", wav_path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        err_lines = [l for l in proc.stderr.splitlines() if not l.startswith("OK:")]
        raise RuntimeError(f"NeuTTS synthesis failed: {chr(10).join(err_lines) or 'unknown error'}")
    if wav_path != path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            conv_cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", path]
            subprocess.run(conv_cmd, check=True, timeout=30, stdin=subprocess.DEVNULL)
            os.remove(wav_path)
        else:
            os.rename(wav_path, path)


# ---------------------------------------------------------------------------
# WAV writer from neutts_synth.py (PCM int16, no soundfile dependency).
# ---------------------------------------------------------------------------
def write_wav_file(path: str, samples, sample_rate: int = 24000) -> None:
    """Write a mono int16 WAV from samples (port of neutts_synth._write_wav).

    Hermes uses numpy for clamp/convert; stdlib-only keeps the frame
    structure identical and clamps via min/max on the int16 range.
    """
    if not isinstance(samples, (list, tuple)):
        samples = list(samples)
    flat = []
    for s in samples:
        if isinstance(s, (list, tuple)):
            flat.extend(s)
        else:
            flat.append(s)
    pcm = bytearray()
    for s in flat:
        s = float(s)
        s = max(-1.0, min(1.0, s))
        pcm += struct.pack("<h", int(round(s * 32767)))
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    data_size = len(pcm)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate,
                            byte_rate, block_align, bits_per_sample))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm)


# ---------------------------------------------------------------------------
# Voice catalog — built from the Edge voices that edge-tts ships by
# default (the Hermes default provider; its voice list lives in the
# edge-tts package, so the common Microsoft neural voices are enumerated
# here for list_voices()).
# ---------------------------------------------------------------------------
EDGE_VOICES = (
    "en-US-AriaNeural", "en-US-ChristopherNeural", "en-US-EricNeural",
    "en-US-GuyNeural", "en-US-JennyNeural", "en-US-MichelleNeural",
    "en-US-RogerNeural", "en-US-SteffanNeural",
    "en-GB-LibbyNeural", "en-GB-RyanNeural", "en-GB-SoniaNeural", "en-GB-ThomasNeural",
    "en-AU-NatashaNeural", "en-AU-WilliamNeural", "en-CA-ClaraNeural", "en-CA-LiamNeural",
    "en-IN-NeerjaNeural", "en-IN-PrabhatNeural", "en-IE-EmilyNeural", "en-IE-ConnorNeural",
    "en-NZ-MollyNeural", "en-NZ-MitchellNeural", "en-ZA-LeahNeural", "en-ZA-LukeNeural",
    "fr-FR-DeniseNeural", "fr-FR-HenriNeural", "de-DE-KatjaNeural", "de-DE-ConradNeural",
    "es-ES-ElviraNeural", "es-ES-AlvaroNeural", "es-MX-DaliaNeural", "es-MX-JorgeNeural",
    "it-IT-ElsaNeural", "it-IT-DiegoNeural", "ja-JP-NanamiNeural", "ja-JP-KeitaNeural",
    "ko-KR-SunHiNeural", "ko-KR-InJoonNeural", "pt-BR-FranciscaNeural", "pt-BR-AntonioNeural",
    "hi-IN-SwaraNeural", "hi-IN-MadhurNeural", "ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural",
    "ar-SA-ZariyahNeural", "ar-SA-HamedNeural", "tr-TR-EmelNeural", "tr-TR-AhmetNeural",
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-TW-HsiaoChenNeural", "zh-HK-HiuMaanNeural",
)

GEMINI_VOICES = (
    "Kore", "Puck", "Charon", "Phero", "Aoede", "Zephyr", "Orus", "Leda",
)

# Default voice per provider (Hermes defaults).
VOICE_BY_PROVIDER = {
    "edge": DEFAULT_EDGE_VOICE,
    "openai": DEFAULT_OPENAI_VOICE,
    "elevenlabs": DEFAULT_ELEVENLABS_VOICE_ID,
    "gemini": DEFAULT_GEMINI_TTS_VOICE,
    "piper": DEFAULT_PIPER_VOICE,
    "xai": DEFAULT_XAI_VOICE_ID,
    "mistral": DEFAULT_MISTRAL_TTS_VOICE_ID,
    "minimax": DEFAULT_MINIMAX_VOICE_ID,
}


def list_voices() -> dict:
    """Return the voice catalog: per-provider default + the static lists.

    Shape: {"ok": True, "voices": [{"provider", "name", "default"}...]}.
    """
    voices = []
    for name in EDGE_VOICES:
        voices.append({"provider": "edge", "name": name, "default": name == DEFAULT_EDGE_VOICE})
    for name in GEMINI_VOICES:
        voices.append({"provider": "gemini", "name": name, "default": name == DEFAULT_GEMINI_TTS_VOICE})
    for provider, default in VOICE_BY_PROVIDER.items():
        if provider in ("edge", "gemini"):
            continue
        voices.append({"provider": provider, "name": default, "default": True})
    return {"ok": True, "voices": voices}


# ---------------------------------------------------------------------------
# The chain entry point.
# ---------------------------------------------------------------------------
def _run_provider(name: str, text: str, voice: str, out_path: str, tts_cfg: dict) -> dict:
    """Run one provider stage; never raises — returns a result dict.

    Mirrors Hermes text_to_speech_tool dispatch: each branch either writes
    the file or raises; we catch and convert to {ok:False, error} so the
    chain can fall through.
    """
    try:
        if name == "gateway":
            return tools.tts(text, voice)
        if name == "edge":
            cfg = tts_cfg.get("edge") or {}
            v = voice or cfg.get("voice") or DEFAULT_EDGE_VOICE
            speed = float(cfg.get("speed", tts_cfg.get("speed", 1.0)))
            _generate_edge(out_path, text, v, speed)
        elif name == "openai":
            _generate_openai(out_path, text, voice, tts_cfg.get("openai") or {}, tts_cfg)
        elif name == "elevenlabs":
            _generate_elevenlabs(out_path, text, voice, tts_cfg.get("elevenlabs") or {})
        elif name == "gemini":
            _generate_gemini(out_path, text, voice, tts_cfg)
        elif name == "piper":
            _generate_piper(out_path, text, voice, tts_cfg)
        elif name == "neutts":
            _generate_neutts(out_path, text, voice, tts_cfg)
        else:
            return {"ok": False, "provider": name or "unset",
                    "error": f"unknown TTS provider: {name}"}
        return {"ok": True, "provider": name}
    except Exception as e:
        return {"ok": False, "provider": name, "error": str(e)}


def _stage_unavailable(name: str) -> dict:
    """Note for a skipped stage (missing binary/config) — mirrors Hermes'
    per-provider 'package not installed' messages."""
    if name == "edge":
        return {"ok": False, "provider": name,
                "error": "edge-tts CLI not found — install with `pip install edge-tts`"}
    if name == "openai":
        return {"ok": False, "provider": name,
                "error": "OPENAI_API_KEY not set"}
    if name == "elevenlabs":
        return {"ok": False, "provider": name,
                "error": "ELEVENLABS_API_KEY not set"}
    if name == "gemini":
        return {"ok": False, "provider": name,
                "error": "GEMINI_API_KEY/GOOGLE_API_KEY not set"}
    if name == "piper":
        return {"ok": False, "provider": name,
                "error": "piper CLI not found — install piper-tts and add it to PATH"}
    if name == "neutts":
        return {"ok": False, "provider": name,
                "error": "neutts CLI not found — install `python -m pip install -U neutts[all]`"}
    if name == "gateway":
        return {"ok": False, "provider": name,
                "error": "NINEROUTER_URL/NINEROUTER_KEY not set"}
    return {"ok": False, "provider": name, "error": "provider unavailable"}


def _should_try_stage(name: str, force_provider: str) -> bool:
    if not force_provider:
        return True
    return name == force_provider


def tts(text: str, voice: str = "", provider: str = "auto") -> dict:
    """Convert *text* to speech; returns ``{"ok", "path"|"url"|"error", "provider", "format"}``.

    Chain (``provider="auto"``): gateway → edge → openai → elevenlabs →
    gemini → piper → neutts. ``provider="edge"`` etc. forces a single
    provider; an explicit ``provider`` that is unconfigured returns
    {ok:False, error} without crashing. Text is normalized through the
    Hermes prepare_spoken_text pipeline, and each provider's per-provider
    character cap truncates long input (Hermes _resolve_max_text_length).
    """
    if not text or not text.strip():
        return {"ok": False, "error": "Text is required"}
    try:
        text = prepare_spoken_text(text, max_chars=None)
    except Exception:
        text = text.strip()
    if not text:
        return {"ok": False, "error": "Text is empty after TTS cleanup"}

    tts_cfg = _load_tts_config()
    speed = tts_cfg.get("speed")
    if speed is not None:
        try:
            tts_cfg = dict(tts_cfg)
            tts_cfg["speed"] = max(0.25, min(4.0, float(speed)))
        except (TypeError, ValueError):
            pass

    force = None
    if provider and provider.lower().strip() != "auto":
        force = provider.lower().strip()
    if not force:
        # Hermes _get_provider returns the DEFAULT_PROVIDER when nothing is
        # configured — but Atropos' chain must default to the auto chain
        # (gateway first), so only an EXPLICITLY configured tts.provider
        # value forces a provider here.
        cfg_provider = ""
        try:
            cfg_provider = str(_load_tts_config().get("provider") or "").strip().lower()
        except Exception:
            pass
        if cfg_provider:
            force = cfg_provider if cfg_provider in BUILTIN_TTS_PROVIDERS else None

    # Truncate with the provider cap (Hermes per-provider caps).
    max_len = _resolve_max_text_length(force or "edge")
    if len(text) > max_len:
        text = text[:max_len]

    if force:
        chain = (force,)
    else:
        chain = AUTO_PROVIDER_CHAIN

    out_dir = detect.atropos_home() / "cache" / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Hermes name: timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f").
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_path = str(out_dir / f"tts_{timestamp}.mp3")

    errors = []
    for name in chain:
        if name == "gateway":
            cfg_ok = bool(_env("NINEROUTER_URL")) and bool(_env("NINEROUTER_KEY"))
        elif name == "edge":
            cfg_ok = bool(_find_edge_tts())
        elif name == "openai":
            cfg_ok = bool(_env("OPENAI_API_KEY") or _env("VOICE_TOOLS_OPENAI_KEY")
                          or (_tts_cfg("openai", {}).get("api_key") or "").strip())
        elif name == "elevenlabs":
            cfg_ok = bool(_env("ELEVENLABS_API_KEY") or (_tts_cfg("elevenlabs", {}).get("api_key") or "").strip())
        elif name == "gemini":
            cfg_ok = bool(_env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"))
        elif name == "piper":
            cfg_ok = bool(shutil.which("piper"))
        elif name == "neutts":
            cfg_ok = bool(shutil.which("neutts"))
        else:
            cfg_ok = True  # unknown name — let _run_provider surface the error
        if not cfg_ok:
            res = _stage_unavailable(name)
            if force:
                # Explicit provider that is unconfigured → clean {ok:False}.
                return {"ok": False, "error": res.get("error") or f"{name} provider unavailable",
                        "provider": name}
            errors.append(res.get("error"))
            continue

        res = _run_provider(name, text, voice, out_path, tts_cfg)
        if res.get("ok"):
            if name == "gateway":
                return {"ok": True, "url": None, "path": None,
                        "provider": "gateway", "format": "audio",
                        "data": res.get("data"), "notes": errors[-3:]}
            # Check the file was actually created (Hermes: "TTS generation
            # produced no output").
            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                res["ok"] = False
                res["error"] = f"TTS generation produced no output (provider: {name})"
            else:
                # Ogg container repair (Hermes _repair_ogg_container) —
                # several backends write MP3/WAV into .ogg paths.
                repaired = _repair_ogg_container(out_path)
                ext = os.path.splitext(repaired)[1].lstrip(".") or "mp3"
                return {"ok": True, "path": repaired, "url": None,
                        "provider": name, "format": ext, "notes": errors[-3:]}
        if res.get("error"):
            errors.append(f"{name}: {res['error']}")
        if force:
            return {"ok": False, "error": res.get("error") or f"{force} provider failed",
                    "provider": name}
    return {"ok": False,
            "error": f"No TTS provider available: {'; '.join(errors[-5:]) or 'no providers configured'}"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Atropos TTS (ported from Hermes tts_tool.py)")
    ap.add_argument("text")
    ap.add_argument("--voice", default="")
    ap.add_argument("--provider", default="auto")
    args = ap.parse_args()
    print(json.dumps(tts(args.text, voice=args.voice, provider=args.provider), indent=2, ensure_ascii=False))