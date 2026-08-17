#!/usr/bin/env python3
"""Atropos vision — Hermes image analysis pipeline, stdlib only.

Ported from real Hermes source with algorithms kept intact:
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\vision_tools.py
    (analyze flow: mime detection, base64 data URL, 20 MB hard ceiling,
    auto-resize ordering/attempts/quality steps, size-error retry-once,
    empty-analysis retry-once, error classification, temp cleanup)

API::

    analyze(image_path, prompt="") -> {"ok", "description", "provider", "elapsed_ms"}
    _resize_dimension_steps(w, h, max_dimension)  # pure, testable

Provider fallback chain:
    1. ``gateway`` — core.tools.vision (9Router /v1/vision), kept FIRST so
       the existing CLI/tests keep working.
    2. ``local`` — OpenAI-compatible chat-completions REST call with the
       Hermes vision message shape (text + image_url data URL; temperature
       0.1, max_tokens 2000, timeout 120 — vision_tools.py:1277-1293),
       model default ``google/gemini-3-flash-preview`` (the Hermes
       docstring default), base URL from ``OPENAICOMPAT_BASE_URL`` /
       ``OPENAI_BASE_URL`` / OpenRouter.

Deliberate deviations (all stdlib-driven):
  - Hermes' auxiliary-LLM router (agent.auxiliary_client) does not exist
    here; the OpenAI-compatible REST call is the local provider.
  - Pillow-based progressive resize is replaced by a minimal stdlib image
    codec (PNG/BMP read + nearest-neighbour resize + PNG write via zlib);
    formats it cannot read (JPEG/GIF/WebP/SVG) get Hermes' actionable
    guidance instead of a silent failure, and the byte/dimension budget
    checks still run first.
  - Region crop: the Hermes source has no region-crop helper (grep
    ``region`` returns nothing) — shape/layout analysis is done via the
    prompt, same as Hermes' own model_tools formatting.
"""
import base64
import binascii
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from . import detect, tools

# Absolute hard ceiling for vision API payloads (20 MB) — above this, no
# major provider accepts the image and we reject outright
# (vision_tools.py _MAX_BASE64_BYTES).
_MAX_BASE64_BYTES = 20 * 1024 * 1024

# Proactive embed cap (4 MB) — the size oversized images are resized DOWN
# to before embedding, regardless of the 20 MB ceiling
# (vision_tools.py _EMBED_TARGET_BYTES).
_EMBED_TARGET_BYTES = 4 * 1024 * 1024

# Proactive embed dimension cap (px, longest side) — Anthropic enforces an
# 8000px per-side ceiling independent of the byte cap (vision_tools.py).
_EMBED_MAX_DIMENSION = 7900

# Target size when auto-resizing on API failure (5 MB) (vision_tools.py).
_RESIZE_TARGET_BYTES = 5 * 1024 * 1024

# Media types the major vision providers accept for inline base64 images.
# Anything outside this set — SVG, BMP, TIFF — is rejected with a
# non-retryable 400 (vision_tools.py _ANTHROPIC_SUPPORTED_MEDIA_TYPES).
_ANTHROPIC_SUPPORTED_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

# Vision LLM call defaults — ported from the vision_analyze_tool body
# (vision_tools.py:1270-1293: timeout 120.0, temperature 0.1, max_tokens 2000).
DEFAULT_VISION_MODEL = "google/gemini-3-flash-preview"
DEFAULT_VISION_TIMEOUT = 120.0
DEFAULT_VISION_TEMPERATURE = 0.1
DEFAULT_VISION_MAX_TOKENS = 2000


# ---------------------------------------------------------------------------
# MIME / encode helpers (Hermes _determine_mime_type / _image_to_base64_data_url
# / _detect_image_mime_type_from_bytes, vision_tools.py:245/519/542).
# ---------------------------------------------------------------------------
def _determine_mime_type(image_path: Path | str) -> str:
    """Determine the MIME type of an image based on its file extension.

    Defaults to image/jpeg if unknown (Hermes original).
    """
    extension = Path(image_path).suffix.lower()
    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml',
    }
    return mime_types.get(extension, 'image/jpeg')


def _detect_image_mime_type_from_bytes(data: bytes) -> str | None:
    """Magic-byte MIME sniff (authoritative, no extension trust).

    Returns None for anything without a recognized image header — SVGs
    included (no magic bytes; the SVG flash check lives in the resolver).
    Port of vision_tools.py _detect_image_mime_type_from_bytes.
    """
    header = data[:64]
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_to_base64_data_url(image_path: Path, mime_type: str | None = None) -> str:
    """Convert an image file to a base64-encoded data URL.

    ``data:image/jpeg;base64,...`` (Hermes original, vision_tools.py:542).
    """
    data = Path(image_path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    mime = mime_type or _determine_mime_type(image_path)
    return f"data:{mime};base64,{encoded}"


def _is_image_size_error(error: Exception) -> bool:
    """Detect if an API error is related to image or payload size.

    Port of vision_tools.py _is_image_size_error (hint list verbatim).
    """
    err_str = str(error).lower()
    return any(hint in err_str for hint in (
        "too large", "payload", "413", "content_too_large",
        "request_too_large", "image_url", "invalid_request",
        "exceeds", "size limit",
    ))


# ---------------------------------------------------------------------------
# Header-based dimension detection — stdlib replacement for Hermes'
# Pillow probe _image_exceeds_dimension (vision_tools.py:607). Hermes
# returns False (no forced resize) when the image can't be read; we
# return None (same conservative outcome) when the header can't be
# parsed with the stdlib codec.
# ---------------------------------------------------------------------------
def _image_dimensions(image_path: Path | str) -> tuple | None:
    """Return ``(width, height)`` from the file headers, or None when the
    format is not stdlib-parseable. Purpose ported from Hermes
    _image_exceeds_dimension: a tall small-byte image can pass every byte
    check yet trip a per-side pixel cap."""
    try:
        head = Path(image_path).read_bytes()
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(head) >= 24:
            return struct.unpack(">II", head[16:24])
    elif head.startswith(b"\xff\xd8\xff"):
        return _jpeg_dimensions(head)
    elif head.startswith((b"GIF87a", b"GIF89a")):
        if len(head) >= 10:
            return struct.unpack("<HH", head[6:10])
    elif head.startswith(b"BM"):
        if len(head) >= 26:
            w = struct.unpack("<i", head[18:22])[0]
            h = struct.unpack("<i", head[22:26])[0]
            return (w, abs(h))
    elif len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        if len(head) >= 30 and head[12:16] == b"VP8X":
            w = 1 + struct.unpack("<I", head[24:27] + b"\x00")[0]
            h = 1 + struct.unpack("<I", head[27:30] + b"\x00")[0]
            return (w, h)
    return None


def _jpeg_dimensions(head: bytes) -> tuple | None:
    """Parse JPEG SOF markers for width/height (0xFFC0..0xFFCF except
    C4/C8/CC); returns None when no SOF is found (progressive files still
    carry an SOF header)."""
    i = 2
    n = len(head)
    while i + 9 < n:
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        if marker == 0xFF or marker in (0x00, 0xD8, 0xD9):
            i += 2
            continue
        if i + 4 > n:
            return None
        seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if seg_len >= 7 and i + 9 <= n:
                # JPEG SOF stores height first; normalize to (width, height).
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return (w, h)
            return None
        i += 2 + seg_len
    return None


# ---------------------------------------------------------------------------
# Downscale ordering — pure port of the attempt loop inside Hermes
# _resize_image_for_vision (vision_tools.py:699-740). Hermes: halve the
# longer side each attempt and scale the shorter side to preserve aspect
# ratio (min dimension 64), re-derive the scale from whichever axis hit
# the floor, stop when dimensions can't shrink further, up to 4 resizing
# rounds; JPEG additionally steps quality 85 → 70 → 50 at each size.
# ---------------------------------------------------------------------------
def _dims_ok(w: int, h: int, max_dimension: int | None) -> bool:
    """True if both pixel dimensions are within the limit (Hermes _dims_ok)."""
    if max_dimension is None:
        return True
    return max(w, h) <= max_dimension


def _resize_dimension_steps(width: int, height: int, max_dimension: int | None = None,
                            *, _allow_byte_path: bool = True) -> list:
    """Return the cumulative (w,h) schedule Hermes' resizer would produce.

    Each entry is the image size after one halving attempt. Empty when no
    shrink is needed (or possible). Pure — no I/O — for direct testing.

    Mirrors Hermes: with a max_dimension set, shrink only while the longest
    side still exceeds it (the byte-budget path always resizes via the
    dimension halving); the 64px floor stops the schedule when the halving
    can't shrink further.
    """
    w, h = width, height
    if max_dimension is not None and _dims_ok(w, h, max_dimension):
        return []  # dimension path already within limits — no resize
    if max_dimension is None and not _allow_byte_path:
        return []  # byte path judge says no shrink needed
    steps: list = []
    for _attempt in range(4):  # Hermes range(5) with attempt 0 = original
        # Proportional scaling: halve both axes (min dimension 64).
        new_w = max(int(w * 0.5), 64)
        new_h = max(int(h * 0.5), 64)
        # Re-derive the scale from whichever dimension hit the floor so
        # both axes shrink by the same factor.
        if new_w == 64 and w > 0:
            effective_scale = 64 / w
            new_h = max(int(h * effective_scale), 64)
        elif new_h == 64 and h > 0:
            effective_scale = 64 / h
            new_w = max(int(w * effective_scale), 64)
        if (new_w, new_h) == (w, h):
            break  # can't shrink further (Hermes stop condition)
        w, h = new_w, new_h
        steps.append((w, h))
        if max_dimension is not None and _dims_ok(w, h, max_dimension):
            break  # dimension budget satisfied — Hermes stops trying
    return steps


def _resize_quality_steps(pil_format: str) -> tuple:
    """Quality schedule per output format (Hermes: quality_steps —
    JPEG 85/70/50, PNG has none — quality only helps JPEG)."""
    return (85, 70, 50) if pil_format.upper() == "JPEG" else (None,)


# ---------------------------------------------------------------------------
# Minimal stdlib image codec (PNG + BMP) — stands in for Pillow so the
# Hermes resize ordering can actually run stdlib-only. PNG: 8-bit gray/RGB/
# RGBA, non-interlaced; BMP: 24/32-bit uncompressed.
# ---------------------------------------------------------------------------
def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    """One PNG chunk: length + type + data + CRC32 (type+data)."""
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", binascii.crc32(ctype + data) & 0xFFFFFFFF))


def decode_image(path: Path | str) -> tuple:
    """Decode an image with the stdlib codec.

    Returns ``(width, height, image)`` where ``image`` is a list of rows
    of ``bytes`` (RGB, 3 bytes per pixel). Raises ValueError for formats
    the stdlib codec cannot read.
    """
    data = Path(path).read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _decode_png(data)
    if data.startswith(b"BM"):
        return _decode_bmp(data)
    mime = _detect_image_mime_type_from_bytes(data)
    raise ValueError(
        f"Image format {(mime or 'unknown')!r} cannot be resized with the "
        "stdlib codec (PNG/BMP only). Convert it to PNG and retry, or "
        "install Pillow for automatic downscaling."
    )


def _decode_png(data: bytes) -> tuple:
    """Decode a non-interlaced 8-bit PNG (gray/RGB/RGBA) to RGB rows."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i = 8
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while i + 8 <= len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        ctype = data[i + 4:i + 8]
        chunk = data[i + 8:i + 8 + length]
        if ctype == b"IHDR":
            if length < 13:
                raise ValueError("corrupt PNG IHDR")
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", chunk[:13])
            if bit_depth != 8 or comp != 0 or filt != 0 or interlace != 0:
                raise ValueError("PNG must be 8-bit, non-interlaced for stdlib resize")
            if color_type not in (0, 2, 6):
                raise ValueError("PNG palette/gray+alpha unsupported by stdlib resize; convert to RGB")
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
        i += 12 + length
    if not width or not height:
        raise ValueError("corrupt PNG (no IHDR/IEND)")
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    bpp = channels
    rows = []
    pos = 0
    prev = bytes(stride)
    for _ in range(height):
        if pos >= len(raw):
            raise ValueError("corrupt PNG (scanlines truncated)")
        f = raw[pos]
        pos += 1
        scan = bytearray(raw[pos:pos + stride])
        pos += stride
        if f == 0:
            pass
        elif f == 1:  # Sub
            for x in range(bpp, stride):
                scan[x] = (scan[x] + scan[x - bpp]) & 0xFF
        elif f == 2:  # Up
            for x in range(stride):
                scan[x] = (scan[x] + prev[x]) & 0xFF
        elif f == 3:  # Average
            for x in range(stride):
                left = scan[x - bpp] if x >= bpp else 0
                scan[x] = (scan[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif f == 4:  # Paeth
            for x in range(stride):
                left = scan[x - bpp] if x >= bpp else 0
                up = prev[x]
                ul = prev[x - bpp] if x >= bpp else 0
                p = left + up - ul
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - ul)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else ul)
                scan[x] = (scan[x] + pred) & 0xFF
        else:
            raise ValueError(f"corrupt PNG (filter {f})")
        rows.append(_png_row_to_rgb(bytes(scan), color_type))
        prev = bytes(scan)
    return width, height, rows


def _png_row_to_rgb(row: bytes, color_type: int) -> bytes:
    out = bytearray()
    if color_type == 0:  # gray
        for b in row:
            out += bytes((b, b, b))
    elif color_type == 2:
        out = bytearray(row)
    else:  # RGBA
        for i in range(0, len(row), 4):
            out += bytes(row[i:i + 3])
    return bytes(out)


def _decode_bmp(data: bytes) -> tuple:
    """Decode an uncompressed 24/32-bit BMP (bottom-up rows) to RGB rows."""
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("not a BMP")
    data_offset = struct.unpack("<I", data[10:14])[0]
    width = struct.unpack("<i", data[18:22])[0]
    height_raw = struct.unpack("<i", data[22:26])[0]
    bits = struct.unpack("<H", data[28:30])[0]
    compression = struct.unpack("<I", data[30:34])[0]
    if bits not in (24, 32) or compression != 0:
        raise ValueError("BMP must be 24/32-bit uncompressed for stdlib resize")
    if width <= 0:
        raise ValueError("corrupt BMP (negative width)")
    flip = height_raw > 0  # positive height = bottom-up rows
    height = abs(height_raw)
    bpp = bits // 8
    stride = ((width * bpp + 3) // 4) * 4
    rows = []
    for r in range(height):
        off = data_offset + (height - 1 - r if flip else r) * stride
        row = data[off:off + width * bpp]
        rgb = bytearray()
        for i in range(0, width * bpp, bpp):
            rgb += bytes((row[i + 2], row[i + 1], row[i]))
        rows.append(bytes(rgb))
    return width, height, rows


def _resize_nearest(rows: list, width: int, height: int, new_w: int, new_h: int) -> list:
    """Nearest-neighbour scale of RGB rows (the cheap, lossless-per-pixel
    stand-in for Hermes' LANCZOS; the Hermes scheduling is preserved by
    _resize_dimension_steps)."""
    out = []
    for y in range(new_h):
        src_y = min(int(y * height / new_h), height - 1)
        src = rows[src_y]
        out.append(bytes(src[min(int(x * width / new_w), width - 1) * 3:
                               min(int(x * width / new_w), width - 1) * 3 + 3]
                         for x in range(new_w)))
    return out


def encode_png(width: int, height: int, rows: list) -> bytes:
    """Encode RGB rows as a PNG (filter 0, zlib, CRC32) — the writer half
    of the stdlib codec."""
    raw = bytearray()
    stride = width * 3
    for row in rows:
        raw.append(0)  # filter: None
        raw.extend(row[:stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def _resize_image_for_vision(image_path: Path, mime_type: str | None = None,
                             max_base64_bytes: int = _RESIZE_TARGET_BYTES,
                             max_dimension: int | None = None) -> str:
    """Convert an image to a base64 data URL, auto-resizing if too large.

    Port of Hermes _resize_image_for_vision (vision_tools.py:625). The
    ordering is copied: quick byte estimate (base64 expands ~4/3 + header)
    and pixel-dimension check first; then halving rounds with the JPEG
    quality schedule; best candidate returned. Pillow is replaced by the
    stdlib PNG/BMP codec; undecodable formats raise the actionable error.
    """
    file_size = Path(image_path).stat().st_size
    estimated_b64 = (file_size * 4) // 3 + 100  # ~header overhead
    needs_resize_for_bytes = estimated_b64 > max_base64_bytes

    needs_resize_for_dims = False
    if max_dimension is not None:
        dims = _image_dimensions(image_path)
        if dims and max(dims) > max_dimension:
            needs_resize_for_dims = True

    if not needs_resize_for_bytes and not needs_resize_for_dims:
        data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        if len(data_url) <= max_base64_bytes:
            return data_url

    mime = mime_type or _determine_mime_type(image_path)

    # Decode with the stdlib codec; formats it cannot read raise with
    # Hermes' guidance (Pillow install hint preserved).
    try:
        width, height, rows = decode_image(image_path)
    except ValueError as e:
        raise ValueError(
            f"Cannot auto-resize {mime}: {e}. Compress the image manually "
            "or install Pillow (`pip install Pillow`) for automatic downscaling."
        ) from e

    # Strategy: halve dimensions until both base64 fits AND pixel dimensions
    # are within limits, up to 4 rounds (Hermes). For PNG, quality is
    # irrelevant — only dimension reduction helps (Hermes comment).
    pil_format = "PNG"
    out_mime = "image/png"
    quality_steps = _resize_quality_steps(pil_format)
    candidate = None
    for attempt, (new_w, new_h) in enumerate([(width, height)] + _resize_dimension_steps(width, height, max_dimension)):
        img = rows if attempt == 0 else _resize_nearest(rows, width, height, new_w, new_h)
        dims = (width, height) if attempt == 0 else (new_w, new_h)
        for q in quality_steps:  # (None,) for PNG
            encoded = encode_png(dims[0], dims[1], img)
            data_url = f"data:{out_mime};base64,{base64.b64encode(encoded).decode('ascii')}"
            candidate = data_url
            if len(data_url) <= max_base64_bytes and _dims_ok(dims[0], dims[1], max_dimension):
                return candidate

    # If we still can't get it small enough, return the best attempt and
    # let the caller decide (Hermes).
    if candidate is not None:
        return candidate
    return data_url


# ---------------------------------------------------------------------------
# Download helper — port of vision_tools.py _download_image /
# _is_retryable_download_error (asyncio loop → urllib, retry classification
# preserved: 4xx other than 429 → fail-fast; timeouts/5xx/429 → retry).
# ---------------------------------------------------------------------------
def _is_retryable_download_error(error: Exception) -> bool:
    """True only for transient image-download failures worth retrying
    (port of vision_tools.py _is_retryable_download_error)."""
    if isinstance(error, (PermissionError, ValueError)):
        return False
    if isinstance(error, urllib.error.HTTPError):
        status = error.code
        if 400 <= status < 500 and status != 429:
            return False
        return True
    return True


def _download_image(image_url: str, destination: Path, max_retries: int = 3) -> Path:
    """Download an image to a local destination with 3-attempt retry
    (port of vision_tools.py _download_image; backoff 2**attempt)."""
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(image_url, timeout=30) as r:
                data = r.read(_MAX_BASE64_BYTES + 1)
            if len(data) > _MAX_BASE64_BYTES:
                raise ValueError("image too large (download exceeds 20 MB)")
            destination.write_bytes(data)
            return destination
        except Exception as e:
            if attempt == max_retries - 1 or not _is_retryable_download_error(e):
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"image download failed: {image_url}")


# ---------------------------------------------------------------------------
# Normalization — port of vision_tools.py _normalize_to_supported_image.
# Supported mimes pass through; SVG (needs a rasterizer) and formats the
# stdlib codec can transcode (BMP → PNG) are handled; the rest raise the
# Hermes error text.
# ---------------------------------------------------------------------------
def _normalize_to_supported_image(image_path: Path, detected_mime: str) -> tuple:
    """Ensure an image is in a vision-provider-supported format.

    Returns ``(path, mime, error)`` — if conversion succeeds the new PNG
    path is a file the CALLER must clean up (Hermes contract). Port of
    vision_tools.py _normalize_to_supported_image with the stdlib codec
    replacing Pillow (SVG rasterization remains impossible stdlib-only).
    """
    if detected_mime in _ANTHROPIC_SUPPORTED_MEDIA_TYPES:
        return image_path, detected_mime, None

    out_dir = detect.atropos_home() / "cache" / "vision"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"converted_{base64.b64encode(os.urandom(6)).decode('ascii').replace('/', 'a')}.png"

    if detected_mime == "image/svg+xml":
        return (
            None, None,
            "This is an SVG, which vision models cannot read directly, and no "
            "SVG rasterizer is installed (tried cairosvg, svglib, rsvg-convert, "
            "inkscape). Convert the SVG to PNG first — e.g. open it in a browser "
            "and screenshot it — then re-run analyze on the PNG.",
        )

    try:
        width, height, rows = decode_image(image_path)
        out_path.write_bytes(encode_png(width, height, rows))
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path, "image/png", None
    except ValueError:
        pass  # fall through to the Hermes-style error
    return (
        None, None,
        f"Image format {detected_mime!r} is not supported by the vision API "
        "and could not be converted to PNG by the stdlib codec (PNG/BMP "
        "only). Convert it to PNG or JPEG and try again.",
    )


# ---------------------------------------------------------------------------
# Local provider — OpenAI-compatible chat completions with the Hermes
# vision message shape (vision_tools.py:1201-1270 message construction).
# ---------------------------------------------------------------------------
def _analyze_local(image_path: Path, prompt: str, mime_type: str,
                   data_url: str, timeout: float = DEFAULT_VISION_TIMEOUT) -> str:
    """Call the OpenAI-compatible vision endpoint.

    Port of Hermes vision_analyze_tool's request half: messages
    [{user: [text, {type: image_url, image_url: {url: data_url}}]}],
    temperature 0.1, max_tokens 2000 (vision_tools.py:1277-1293).
    On a size-related rejection while above the 5 MB target, auto-resize
    and retry once (vision_tools.py:1295-1313); on empty analysis, retry
    once (vision_tools.py:1318-1324).
    """
    cfg = _vision_cfg()
    model = cfg.get("model") or os.environ.get("OPENAI_VISION_MODEL") or DEFAULT_VISION_MODEL
    base_url = (cfg.get("base_url") or os.environ.get("OPENAICOMPAT_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL") or "https://openrouter.ai/api/v1")
    base_url = base_url.rstrip("/")
    api_key = (cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY") or "")
    if not api_key:
        raise RuntimeError("no vision API key configured (OPENAI_API_KEY / OPENROUTER_API_KEY)")

    def _call(url: str) -> str:
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }],
            "temperature": DEFAULT_VISION_TEMPERATURE,
            "max_tokens": DEFAULT_VISION_MAX_TOKENS,
        }
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        return _extract_analysis(body)

    # Try full-size image first; on size-related rejection, downscale and
    # retry once (Hermes).
    try:
        analysis = _call(data_url)
    except Exception as api_err:
        if _is_image_size_error(api_err) and len(data_url) > _RESIZE_TARGET_BYTES:
            data_url = _resize_image_for_vision(image_path, mime_type=mime_type)
            analysis = _call(data_url)
        else:
            raise

    # Retry once on empty content (reasoning-only response) (Hermes).
    if not analysis:
        analysis = _call(data_url)
    return analysis or "There was a problem with the request and the image could not be analyzed."


def _extract_analysis(body: bytes) -> str:
    """Extract content (falling back to reasoning) from a chat-completions
    response — port of Hermes extract_content_or_reasoning."""
    data = json.loads(body.decode("utf-8"))
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"vision response was malformed: {e}") from e
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    reasoning = message.get("reasoning") or choice.get("reasoning_content") or ""
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()
    return ""


def _vision_cfg() -> dict:
    """The ``vision`` section of settings (Atropos analog of the Hermes
    auxiliary.vision config block)."""
    try:
        from . import settings as _s
        cfg = _s.get("vision")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _classify_vision_error(e: Exception, model: str) -> str:
    """Human-usable analysis text for a failed call — port of the except
    branch of Hermes vision_analyze_tool (vision_tools.py:1352-1383)."""
    err_str = str(e).lower()
    if any(hint in err_str for hint in (
        "402", "insufficient", "payment required", "credits", "billing",
    )):
        return (
            "Insufficient credits or payment required. Please top up your "
            f"API provider account and try again. Error: {e}"
        )
    if any(hint in err_str for hint in (
        "does not support", "not support image",
        "content_policy", "multimodal",
        "unrecognized request argument", "image input",
    )):
        return (
            f"{model} does not support vision or our request was not "
            f"accepted by the server. Error: {e}"
        )
    if "invalid_request" in err_str or "image_url" in err_str:
        return (
            "The vision API rejected the image. This can happen when the "
            "image is in an unsupported format, corrupted, or still too "
            "large after auto-resize. Try a smaller JPEG/PNG and retry. "
            f"Error: {e}"
        )
    return (
        "There was a problem with the request and the image could not "
        f"be analyzed. Error: {e}"
    )


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
def analyze(image_path: str, prompt: str = "") -> dict:
    """Analyze an image file (or URL); returns
    ``{"ok", "description", "provider", "elapsed_ms"}``.

    Chain: ``gateway`` (core.tools.vision, 9Router /v1/vision) first,
    then the local OpenAI-compatible provider. A missing file, an
    unconfigured chain, or a failed call yields ``{"ok": False, "error"}``
    — never a crash and never a fabricated description.
    """
    started = time.monotonic()
    if not prompt:
        prompt = "Describe this image"
    if not image_path or not str(image_path).strip():
        return {"ok": False, "error": "image path is required",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    p = Path(image_path)
    is_url = urllib.parse.urlparse(str(image_path)).scheme in ("http", "https")
    temp_files: list = []
    try:
        # URL images are downloaded with Hermes retry semantics; local
        # files are used directly and never deleted (Hermes note).
        if is_url:
            cache_dir = detect.atropos_home() / "cache" / "vision"
            cache_dir.mkdir(parents=True, exist_ok=True)
            p = cache_dir / f"temp_image_{base64.b64encode(os.urandom(8)).decode('ascii').replace('/', 'a')}.img"
            _download_image(image_path, p)
            temp_files.append(p)
        elif not p.exists():
            return {"ok": False, "error": f"file not found: {image_path}",
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}

        data = p.read_bytes()
        detected_mime = _detect_image_mime_type_from_bytes(data) or _determine_mime_type(p)

        # Normalize unsupported formats (SVG, BMP, ...) to PNG before
        # encoding (Hermes _normalize_to_supported_image).
        normalized, mime_type, norm_err = _normalize_to_supported_image(p, detected_mime)
        if norm_err or normalized is None:
            return {"ok": False, "error": norm_err or "Image normalization failed.",
                    "provider": "normalize",
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}
        if normalized != p:
            temp_files.append(normalized)
            p = normalized

        image_data_url = _image_to_base64_data_url(p, mime_type=mime_type)

        # Hard limit (20 MB) — no provider accepts payloads this large;
        # resize down to 5 MB target before giving up (Hermes).
        if len(image_data_url) > _MAX_BASE64_BYTES:
            image_data_url = _resize_image_for_vision(p, mime_type=mime_type)
            if len(image_data_url) > _MAX_BASE64_BYTES:
                return {"ok": False,
                        "error": (f"Image too large for vision API: base64 payload is "
                                  f"{len(image_data_url) / (1024 * 1024):.1f} MB "
                                  f"(limit {_MAX_BASE64_BYTES / (1024 * 1024):.0f} MB) even after "
                                  f"resizing. Convert the image to PNG manually or compress it."),
                        "provider": "resize",
                        "elapsed_ms": int((time.monotonic() - started) * 1000)}

        # 1) Gateway provider (9Router) — first so existing CLI/tests stay green.
        gw = tools.vision(str(p), prompt)
        if gw.get("ok"):
            data_d = gw.get("data")
            if isinstance(data_d, dict):
                if data_d.get("success") is False:
                    return {"ok": False, "error": data_d.get("error") or data_d.get("analysis") or "gateway vision failed",
                            "provider": "gateway",
                            "elapsed_ms": int((time.monotonic() - started) * 1000)}
                if data_d.get("analysis"):
                    return {"ok": True, "description": str(data_d["analysis"]),
                            "provider": "gateway",
                            "elapsed_ms": int((time.monotonic() - started) * 1000)}
                if data_d.get("description"):
                    return {"ok": True, "description": str(data_d["description"]),
                            "provider": "gateway",
                            "elapsed_ms": int((time.monotonic() - started) * 1000)}
            return {"ok": False, "error": "gateway vision returned no analysis",
                    "provider": "gateway",
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}

        # 2) Local OpenAI-compatible provider.
        model = _vision_cfg().get("model") or os.environ.get("OPENAI_VISION_MODEL") or DEFAULT_VISION_MODEL
        try:
            analysis = _analyze_local(p, prompt, mime_type, image_data_url)
            return {"ok": True, "description": analysis, "provider": "local",
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "description": _classify_vision_error(e, model),
                    "provider": "local",
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}
    finally:
        # Clean up temporary files (URL downloads + converted copies) —
        # local inputs are never deleted (Hermes finally block).
        for temp_file in temp_files:
            try:
                temp_file.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Atropos vision (ported from Hermes vision_tools.py)")
    ap.add_argument("image")
    ap.add_argument("--prompt", default="Describe this image")
    args = ap.parse_args()
    print(json.dumps(analyze(args.image, prompt=args.prompt), indent=2, ensure_ascii=False))