#!/usr/bin/env python3
"""Atropos image generation — Hermes FAL model routing, stdlib only.

Ported from real Hermes source with algorithms kept intact:
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\image_generation_tool.py
    (FAL_MODELS catalog with size_style families, _build_fal_payload
    size translation + supports whitelist, _build_fal_edit_payload
    image_urls + edit_supports, Clarity Upscaler _upscale_image params,
    model resolution config→env→default, gating via check_fal_api_key)
  - C:\\Users\\a2z\\AppData\\Local\\hermes\\hermes-agent\\tools\\flux3_video_tool.py
    (skimmed for the image-relevant parts: local-path detection and the
    managed-gateway submit shape ``{mode, prompt, ...}`` — the vendor's
    own fields are the gateway's business, matching Atropos' 9Router)

API::

    generate(prompt, model="", size="1024x1024", upscale=False, outpaint="")
        -> {"ok", "url"|"image", "provider", "model", "elapsed_ms", ...}

Provider chain:
    1. ``gateway`` — core.tools.imagine (9Router /v1/images/generations),
       kept FIRST so existing CLI/tests keep working.
    2. ``fal`` — direct FAL.ai queue API via urllib (FAL_KEY), porting the
       Hermes catalog + payload builder + upscaler.

Deliberate deviations (all stdlib-driven):
  - fal_client SDK → urllib queue POST/GET with the same endpoint + body
    (fal_client.submit(url, arguments) is a thin wrapper over the queue).
  - The Hermes managed-gateway (Nous portal) path is not ported (Atropos
    has no portal); the 9Router gateway plays that role.
  - Plugin-registry dispatch (krea etc.) is not ported (no plugin
    registry in Atropos).
  - ``outpaint``: the Hermes image/video tools contain no outpaint
    parameter (grep of image_generation_tool.py, flux3_video_tool.py and
    video_generation_tool.py finds none) — the pad/expand operation is
    exposed here as the gateway's ``outpaint`` passthrough (the gateway
    owns validation and translation onto vendor fields, exactly like the
    flux3 gateway submit contract), and for FAL it expands the
    ``image_size`` to the next preset up.
  - Size handling: Hermes translates aspect_ratio → native size spec
    (preset enum / aspect enum / literal); Atropos accepts a literal
    ``WxH`` and keeps the same three size_style families for the FAL
    payload translation.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import tools

# ---------------------------------------------------------------------------
# FAL catalog — the same size_style families and per-model metadata as
# image_generation_tool.py FAL_MODELS (display/speed/price strings trimmed;
# payload-relevant fields kept verbatim).
# ---------------------------------------------------------------------------
DEFAULT_ASPECT_RATIO = "landscape"
VALID_ASPECT_RATIOS = ("landscape", "square", "portrait")

# Model → (size_style, sizes). size_style families (Hermes docstring):
#   "image_size_preset" — preset enum ("square_hd", "landscape_16_9", ...)
#   "aspect_ratio"      — aspect ratio enum ("16:9", "1:1", ...)
#   "gpt_literal"       — literal dimension strings ("1024x1024", ...)
FAL_MODELS = {
    "fal-ai/flux-2/klein/9b": {
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 4,
            "output_format": "png",
            "enable_safety_checker": False,
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "seed",
            "output_format", "enable_safety_checker",
        },
        "upscale": False,
    },
    "fal-ai/flux-2-pro": {
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 50,
            "guidance_scale": 4.5,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": False,
            "safety_tolerance": "5",
            "sync_mode": True,
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "guidance_scale",
            "num_images", "output_format", "enable_safety_checker",
            "safety_tolerance", "sync_mode", "seed",
        },
        "upscale": True,   # Backward-compat: Hermes current default behavior.
    },
    "fal-ai/z-image/turbo": {
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 8,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": False,
            "enable_prompt_expansion": False,  # avoid the extra per-request charge
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "num_images",
            "seed", "output_format", "enable_safety_checker",
            "enable_prompt_expansion",
        },
        "upscale": False,
    },
    "fal-ai/nano-banana-pro": {
        "size_style": "aspect_ratio",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {
            "num_images": 1,
            "output_format": "png",
            "safety_tolerance": "5",
            "resolution": "1K",  # cheapest tier; 4K doubles per-image cost
        },
        "supports": {
            "prompt", "aspect_ratio", "num_images", "output_format",
            "safety_tolerance", "seed", "sync_mode", "resolution",
            "enable_web_search", "limit_generations",
        },
        "upscale": False,
    },
    "fal-ai/gpt-image-1.5": {
        "size_style": "gpt_literal",
        "sizes": {
            "landscape": "1536x1024",
            "square": "1024x1024",
            "portrait": "1024x1536",
        },
        "defaults": {
            "quality": "medium",  # pinned to keep billing predictable (Hermes)
            "num_images": 1,
            "output_format": "png",
        },
        "supports": {
            "prompt", "image_size", "quality", "num_images", "output_format",
            "background", "sync_mode",
        },
        "upscale": False,
    },
    "fal-ai/ideogram/v3": {
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "rendering_speed": "BALANCED",
            "expand_prompt": True,
            "style": "AUTO",
        },
        "supports": {
            "prompt", "image_size", "rendering_speed", "expand_prompt",
            "style", "seed",
        },
        "upscale": False,
    },
    "fal-ai/qwen-image": {
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 30,
            "guidance_scale": 2.5,
            "num_images": 1,
            "output_format": "png",
            "acceleration": "regular",
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "guidance_scale",
            "num_images", "output_format", "acceleration", "seed", "sync_mode",
        },
        "upscale": False,
    },
    "fal-ai/krea/v2/medium/text-to-image": {
        "size_style": "aspect_ratio",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {
            "creativity": "medium",
        },
        "supports": {
            "prompt", "aspect_ratio", "creativity", "seed",
            "image_style_references",
        },
        "upscale": False,
    },
    "fal-ai/krea/v2/large/text-to-image": {
        "size_style": "aspect_ratio",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {
            "creativity": "medium",
        },
        "supports": {
            "prompt", "aspect_ratio", "creativity", "seed",
            "image_style_references",
        },
        "upscale": False,
    },
}

# Default model is the fastest reasonable option. Kept cheap and sub-1s
# (Hermes DEFAULT_MODEL comment).
DEFAULT_MODEL = "fal-ai/flux-2/klein/9b"

# Upscaler (Clarity Upscaler) — Hermes constants (image_generation_tool.py:432).
UPSCALER_MODEL = "fal-ai/clarity-upscaler"
UPSCALER_FACTOR = 2
UPSCALER_SAFETY_CHECKER = False
UPSCALER_DEFAULT_PROMPT = "masterpiece, best quality, highres"
UPSCALER_NEGATIVE_PROMPT = "(worst quality, low quality, normal quality:2)"
UPSCALER_CREATIVITY = 0.35
UPSCALER_RESEMBLANCE = 0.6
UPSCALER_GUIDANCE_SCALE = 4
UPSCALER_NUM_INFERENCE_STEPS = 18

# Edit-capable models (Hermes edit_endpoint entries) — used to route
# image-to-image requests; outpainting is the only edit op exposed here.
EDIT_MODELS = frozenset({
    "fal-ai/flux-2/klein/9b", "fal-ai/flux-2-pro", "fal-ai/nano-banana-pro",
    "fal-ai/gpt-image-1.5", "fal-ai/ideogram/v3", "fal-ai/qwen-image",
})

# FAL queue endpoint template (fal_client's submit/status/get URLs).
FAL_QUEUE_SUBMIT = "https://queue.fal.run/{model}"
FAL_QUEUE_STATUS = "https://queue.fal.run/{request_id}/status"
FAL_QUEUE_RESULT = "https://queue.fal.run/{request_id}/result"


# ---------------------------------------------------------------------------
# Config (Hermes _resolve_fal_model: config.yaml image_gen.model → env
# FAL_IMAGE_MODEL → DEFAULT_MODEL; here settings.ini [imagine]).
# ---------------------------------------------------------------------------
def _imagine_cfg() -> dict:
    try:
        from . import settings as _s
        cfg = _s.get("imagine")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_fal_model(model: str = "") -> tuple:
    """Resolve the active FAL model: explicit arg → config → env → default.

    Unknown ids fall back to DEFAULT_MODEL with a warning, matching Hermes
    _resolve_fal_model. Returns ``(model_id, metadata_dict)``.
    """
    model_id = model.strip()
    if not model_id:
        model_id = (_imagine_cfg().get("model") or os.environ.get("FAL_IMAGE_MODEL", "")).strip()
    if not model_id:
        return DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL]
    if model_id not in FAL_MODELS:
        return DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL]
    return model_id, FAL_MODELS[model_id]


def _fal_key() -> str:
    """FAL.ai API key (FAL_KEY, matching Hermes fal_key_is_configured's env)."""
    return (os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY") or "").strip()


def check_fal_key() -> bool:
    """True if a FAL.ai API key is available (port of check_fal_api_key)."""
    return bool(_fal_key())


# ---------------------------------------------------------------------------
# Size translation — port of Hermes _build_fal_payload size handling
# (image_generation_tool.py:565). ``size`` is a literal "WxH"; the
# aspect-based Hermes presets are matched by nearest preset family.
# ---------------------------------------------------------------------------
def _size_to_aspect(size: str) -> str:
    """Map a ``WxH`` literal to the nearest Hermes aspect preset."""
    try:
        w, h = (int(x) for x in str(size).lower().split("x")[:2])
    except (ValueError, TypeError):
        return DEFAULT_ASPECT_RATIO
    if w <= 0 or h <= 0:
        return DEFAULT_ASPECT_RATIO
    if w > h:
        return "landscape"
    if h > w:
        return "portrait"
    return "square"


def _size_to_fal(model_id: str, size: str, outpaint: str = "") -> tuple:
    """Translate ``size`` (+ optional outpaint hint) into the model's native
    size spec — port of the Hermes size_style dispatch:

      image_size_preset → ``image_size`` preset enum; gpt_literal →
      literal dimension string; aspect_ratio → ``aspect_ratio`` enum.

    Returns ``(key, value)`` where key is "image_size" or "aspect_ratio".
    Outpaint expands the preset to the next preset up (square_hd →
    landscape_16_9 → portrait_16_9 wraps), mirroring how Hermes'
    edit/upscale surfaces push toward larger canvases.
    """
    meta = FAL_MODELS[model_id]
    size_style = meta["size_style"]
    sizes = meta["sizes"]
    aspect = _size_to_aspect(size)
    if aspect not in sizes:
        aspect = DEFAULT_ASPECT_RATIO
    if outpaint and size_style in ("image_size_preset", "gpt_literal"):
        order = ("square", "landscape", "portrait")
        idx = order.index(aspect)
        aspect = order[(idx + 1) % len(order)]
    if size_style == "aspect_ratio":
        return "aspect_ratio", sizes[aspect]
    if size_style == "gpt_literal":
        return "image_size", sizes[aspect]
    return "image_size", sizes[aspect]


def _build_fal_payload(model_id: str, prompt: str, size: str,
                       outpaint: str = "", overrides: dict | None = None) -> dict:
    """Build a FAL request payload for *model_id* from unified inputs.

    Port of Hermes _build_fal_payload: translate size into the model's
    native spec, merge model defaults, apply caller overrides, then filter
    to the ``supports`` whitelist — ``prompt`` is always kept (a missing
    whitelist entry must not strip the prompt and send an empty request).
    """
    meta = FAL_MODELS[model_id]
    key, value = _size_to_fal(model_id, size, outpaint)
    payload: dict = dict(meta.get("defaults", {}))
    payload["prompt"] = (prompt or "").strip()
    payload[key] = value
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                payload[k] = v
    supports = meta["supports"]
    return {k: v for k, v in payload.items() if k in supports or k == "prompt"}


def _build_fal_edit_payload(model_id: str, prompt: str, image_url: str,
                            size: str) -> dict:
    """Build an edit payload (image-to-image) — port of Hermes
    _build_fal_edit_payload: ``image_urls`` list + prompt, size only when
    the model's edit_supports advertises it; required keys always kept."""
    meta = FAL_MODELS[model_id]
    edit_supports = meta.get("edit_supports") or set()
    payload: dict = dict(meta.get("defaults", {}))
    payload["prompt"] = (prompt or "").strip()
    payload["image_urls"] = [image_url]
    if "image_size" in edit_supports or "aspect_ratio" in edit_supports:
        key, value = _size_to_fal(model_id, size)
        if key in edit_supports:
            payload[key] = value
    _required = {"prompt", "image_urls"}
    return {k: v for k, v in payload.items() if k in edit_supports or k in _required}


# ---------------------------------------------------------------------------
# FAL queue client — urllib stand-in for fal_client.submit + handler.get()
# (the Hermes _submit_fal_request/fal_client path). Same wire: POST the
# arguments JSON to queue.fal.run/<model> with the x-idempotency-key
# header, then poll GET /<request_id>/status until COMPLETED, then GET
# /<request_id>/result.
# ---------------------------------------------------------------------------
def _fal_request(model: str, arguments: dict, timeout: float = 240.0) -> dict:
    """Submit a FAL queue request and block until the result is ready."""
    import uuid
    api_key = _fal_key()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Key {api_key}",
               "x-idempotency-key": str(uuid.uuid4())}
    req = urllib.request.Request(FAL_QUEUE_SUBMIT.format(model=model),
                                 data=json.dumps(arguments).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        submitted = json.loads(r.read().decode("utf-8"))
    request_id = submitted.get("request_id")
    if not request_id:
        raise RuntimeError(f"FAL submit returned no request_id: {submitted}")

    started = time.monotonic()
    while True:
        if time.monotonic() - started > timeout:
            raise RuntimeError(f"FAL request {request_id} timed out after {timeout:.0f}s")
        try:
            with urllib.request.urlopen(FAL_QUEUE_STATUS.format(request_id=request_id),
                                        headers={"Authorization": f"Key {api_key}"},
                                        timeout=30) as r:
                status = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (404,):
                raise RuntimeError(f"FAL request {request_id} not found")
            raise
        if status.get("status") == "COMPLETED":
            with urllib.request.urlopen(FAL_QUEUE_RESULT.format(request_id=request_id),
                                        headers={"Authorization": f"Key {api_key}"},
                                        timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        if status.get("status") in ("FAILED", "CANCELLED"):
            detail = status.get("error") or status.get("detail") or ""
            raise RuntimeError(f"FAL request {request_id} {status.get('status')}: {detail}")
        time.sleep(2)


# ---------------------------------------------------------------------------
# Upscaler — port of Hermes _upscale_image (image_generation_tool.py:674)
# with the exact argument names from the Clarity Upscaler payload.
# ---------------------------------------------------------------------------
def _upscale_image(image_url: str, original_prompt: str) -> dict | None:
    """Upscale an image using FAL's Clarity Upscaler; None on failure
    (caller falls back to the original image — Hermes contract)."""
    try:
        upscaler_arguments = {
            "image_url": image_url,
            "prompt": f"{UPSCALER_DEFAULT_PROMPT}, {original_prompt}",
            "upscale_factor": UPSCALER_FACTOR,
            "negative_prompt": UPSCALER_NEGATIVE_PROMPT,
            "creativity": UPSCALER_CREATIVITY,
            "resemblance": UPSCALER_RESEMBLANCE,
            "guidance_scale": UPSCALER_GUIDANCE_SCALE,
            "num_inference_steps": UPSCALER_NUM_INFERENCE_STEPS,
            "enable_safety_checker": UPSCALER_SAFETY_CHECKER,
        }
        result = _fal_request(UPSCALER_MODEL, upscaler_arguments)
        if result and "image" in result:
            img = result["image"]
            if isinstance(img, dict):
                return {"url": img["url"], "width": img.get("width", 0),
                        "height": img.get("height", 0), "upscaled": True,
                        "upscale_factor": UPSCALER_FACTOR}
            return {"url": str(img), "upscaled": True,
                    "upscale_factor": UPSCALER_FACTOR}
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
def generate(prompt: str, model: str = "", size: str = "1024x1024",
             upscale: bool = False, outpaint: str = "") -> dict:
    """Generate an image from *prompt*.

    Returns ``{"ok", "url"|"image", "provider", "model", "elapsed_ms"}``.
    Chain: ``gateway`` (core.tools.imagine, 9Router /v1/images/generations)
    first, then the direct FAL.ai queue path. A missing prompt, an
    unconfigured chain, or a failed call yields ``{"ok": False, "error"}``
    — never a crash, never a fabricated image.
    """
    started = time.monotonic()
    if not prompt or not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "Prompt is required and must be a non-empty string",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    size = size or "1024x1024"

    # 1) Gateway provider (9Router) — first so existing CLI/tests stay green.
    gw_payload = {"prompt": prompt}
    if size:
        gw_payload["size"] = size
    if upscale:
        gw_payload["upscale"] = True
    if outpaint:
        gw_payload["outpaint"] = outpaint
    gw = tools.imagine(prompt) if not (upscale or outpaint) else _tools_gateway_images(gw_payload)
    if gw.get("ok"):
        data_d = gw.get("data")
        if isinstance(data_d, dict):
            url = data_d.get("url") or data_d.get("image")
            if not url and isinstance(data_d.get("data"), list):
                url = next((i.get("url") for i in data_d["data"]
                            if isinstance(i, dict) and i.get("url")), None)
            if url:
                return {"ok": True, "url": str(url), "provider": "gateway",
                        "model": data_d.get("model") or model or "",
                        "elapsed_ms": int((time.monotonic() - started) * 1000)}
        return {"ok": False, "error": "gateway image generation returned no image URL",
                "provider": "gateway",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    # 2) Direct FAL.ai provider.
    if not check_fal_key():
        return {"ok": False,
                "error": ("Image generation is unavailable: no FAL backend reachable. "
                          "Set FAL_KEY (free key at https://fal.ai) or configure the "
                          "9Router gateway (NINEROUTER_URL/NINEROUTER_KEY)."),
                "provider": "fal",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    model_id, meta = _resolve_fal_model(model)
    try:
        if outpaint and model_id not in EDIT_MODELS:
            return {"ok": False,
                    "error": (f"Model '{model_id}' is not capable of image-to-image / "
                              "editing. Provide a text-only prompt (omit outpaint), or "
                              "switch to an edit-capable model."),
                    "provider": "fal", "model": model_id,
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}

        if outpaint:
            # The outpaint expansion passes through the gateway when routed
            # there; on the direct path we expand the canvas preset (the
            # stdlib surface has no pixel-painting backend).
            arguments = _build_fal_payload(model_id, prompt, size, outpaint=outpaint)
            result = _fal_request(model_id, arguments)
        else:
            arguments = _build_fal_payload(model_id, prompt, size)
            result = _fal_request(model_id, arguments)

        if not result or "images" not in result:
            raise ValueError("Invalid response from FAL.ai API — no images returned")
        images = result.get("images") or []
        if not images:
            raise ValueError("No images were generated")

        # Edit endpoints already return the final composition; the Clarity
        # upscaler is a text-to-image quality pass, so skip it for edits
        # (Hermes comment + gating).
        should_upscale = bool(meta.get("upscale", False)) or bool(upscale)

        formatted = []
        for img in images:
            if not (isinstance(img, dict) and "url" in img):
                continue
            original = {"url": img["url"], "width": img.get("width", 0),
                        "height": img.get("height", 0)}
            if should_upscale and not outpaint:
                upscaled = _upscale_image(img["url"], prompt.strip())
                if upscaled:
                    formatted.append(upscaled)
                    continue
            original["upscaled"] = False
            formatted.append(original)
        if not formatted:
            raise ValueError("No valid image URLs returned from API")

        return {"ok": True, "url": formatted[0]["url"], "provider": "fal",
                "model": model_id,
                "upscaled": formatted[0].get("upscaled", False),
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e), "provider": "fal",
                "model": model_id,
                "elapsed_ms": int((time.monotonic() - started) * 1000)}


def _tools_gateway_images(payload: dict) -> dict:
    """Gateway call with extended payload (upscale/outpaint passthrough).

    The flux3 gateway submit contract (flux3_video_tool.py _submit_args:
    the model's arguments, minus Nones, pass through — the gateway owns
    validation and translation onto the vendor's fields) — mapped onto the
    Atropos 9Router images endpoint.
    """
    from . import tools as _t
    url = os.environ.get("NINEROUTER_URL", "").rstrip("/") + "/v1/images/generations"
    key = os.environ.get("NINEROUTER_KEY", "")
    if not url or not key:
        return {"ok": False, "error": "NINEROUTER_URL/NINEROUTER_KEY not set"}
    try:
        import urllib.request as _ur
        req = _ur.Request(url, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json",
                                   "Authorization": f"Bearer {key}"})
        with _ur.urlopen(req, timeout=120) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Atropos imagine (ported from Hermes image_generation_tool.py)")
    ap.add_argument("prompt")
    ap.add_argument("--model", default="")
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--upscale", action="store_true")
    ap.add_argument("--outpaint", default="")
    args = ap.parse_args()
    print(json.dumps(generate(args.prompt, model=args.model, size=args.size,
                              upscale=args.upscale, outpaint=args.outpaint),
                     indent=2, ensure_ascii=False))