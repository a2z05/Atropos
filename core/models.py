#!/usr/bin/env python3
"""Atropos universal models — one registry over every harness's providers.

Harnesses (Hermes, Claude) each carry their own notion of which model is
served where. This module keeps a single canonical list at
``~/.atropos/models.json``::

    {
      "entries": [
        {
          "name": "deepmo",
          "provider": "nain",
          "model": "deepmo",
          "base_url": "",
          "api_key_env": "OPENAI_API_KEY",
          "enabled": true,
          "mode": "shared"
        }
      ],
      "assignments": {"hermes": "deepmo", "claude": "gpt-4o"}
    }

``assignments`` maps each harness to the model name it should use.
``active(harness)`` resolves the assignment → the matching entry → a
router.get()-shaped dict ``{model, base_url, api_key_env}``. A model with
no base_url inherits the provider's base_url when the provider is one of
the known routers (router.ROUTERS).

Default seed mirrors router.ROUTERS (nain/deepmo, omni/gpt-4o,
local/llama3). Pure stdlib; never imports core.dashboard (circular).
"""
import json
import re
from copy import deepcopy
from pathlib import Path

from . import detect, router

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

MODES = ("shared", "per-harness", "atropos-only")
HARNESSES = ("hermes", "claude", "atropos")

# name → {provider, model, base_url, api_key_env} — mirrors router.ROUTERS.
DEFAULT_SEED = [
    {
        "name": "deepmo",
        "provider": "nain",
        "model": "deepmo",
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY",
    },
    {
        "name": "gpt-4o",
        "provider": "omni",
        "model": "gpt-4o",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    {
        "name": "llama3",
        "provider": "local",
        "model": "llama3",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_HOST",
    },
]


def valid_name(name: str) -> bool:
    """True when ``name`` is a safe model identifier (no path tricks)."""
    return bool(name and NAME_RE.fullmatch(name))


def store_path() -> Path:
    """Canonical store file (~/.atropos/models.json)."""
    return detect.atropos_home() / "models.json"


def _load() -> dict:
    """Load the store; missing/corrupt files fall back to the seed."""
    p = store_path()
    if not p.exists():
        return _seed_store()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _seed_store()
    if not isinstance(data, dict):
        return _seed_store()
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        assignments = {}
    return {"entries": [e for e in entries if isinstance(e, dict)],
            "assignments": assignments}


def _seed_store() -> dict:
    """Fresh store: default seed entries + empty assignments."""
    entries = [dict(e) | {"enabled": True, "mode": "shared"} for e in DEFAULT_SEED]
    return {"entries": entries, "assignments": {}}


def _save(data: dict):
    """Write the store, creating ~/.atropos on demand."""
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _entry(entries: list, name: str) -> dict | None:
    """Find a store entry by name."""
    for e in entries:
        if e.get("name") == name:
            return e
    return None


def _provider_base(provider: str) -> str:
    """Provider's known base_url from router.ROUTERS, or empty."""
    rinfo = router.ROUTERS.get(provider)
    return rinfo.get("base_url", "") if rinfo else ""


def _provider_key_env(provider: str) -> str:
    """Provider's known api_key_env from router.ROUTERS, or empty."""
    rinfo = router.ROUTERS.get(provider)
    return rinfo.get("api_key_env", "") if rinfo else ""


# ── CRUD ──────────────────────────────────────────────────────────────────
def list_models() -> list:
    """All store entries, one dict per model."""
    return deepcopy(_load()["entries"])


def _normalize_entry(e: dict, name: str) -> dict:
    """Fill provider defaults for a new model entry."""
    provider = e.get("provider") or ""
    entry = {
        "name": name,
        "provider": provider,
        "model": e.get("model") or name,
        "base_url": e.get("base_url") or (_provider_base(provider) if provider else ""),
        "api_key_env": e.get("api_key_env") or (_provider_key_env(provider) if provider else ""),
        "enabled": bool(e.get("enabled", True)),
        "mode": e.get("mode") if e.get("mode") in MODES else "shared",
    }
    rinfo = router.ROUTERS.get(provider) if provider else None
    if rinfo:
        # provider is a known router: inherit its canonical model/url/key env
        entry["model"] = e.get("model") or rinfo.get("model") or name
        entry["base_url"] = e.get("base_url") or rinfo.get("base_url", "")
        entry["api_key_env"] = e.get("api_key_env") or rinfo.get("api_key_env", "")
    return entry


def add(name: str, provider: str = "", model: str = "", base_url: str = "",
        api_key_env: str = "", mode: str = "shared") -> dict:
    """Register a model entry. Raises ValueError on invalid input/duplicates."""
    if not valid_name(name):
        raise ValueError(f"invalid model name: {name!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    data = _load()
    if _entry(data["entries"], name) is not None:
        raise ValueError(f"model already registered: {name}")
    entry = _normalize_entry({
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "enabled": True,
        "mode": mode,
    }, name)
    data["entries"].append(entry)
    _save(data)
    return deepcopy(entry)


def remove(name: str) -> dict:
    """Remove a model entry (any harness assignment to it is cleared)."""
    if not valid_name(name):
        raise ValueError(f"invalid model name: {name!r}")
    data = _load()
    if _entry(data["entries"], name) is None:
        raise FileNotFoundError(f"model not found: {name}")
    data["entries"] = [e for e in data["entries"] if e.get("name") != name]
    for h in HARNESSES:
        if data["assignments"].get(h) == name:
            data["assignments"].pop(h, None)
    _save(data)
    return {"ok": True, "name": name, "removed": True}


def enable(name: str) -> dict:
    """Enable a model entry."""
    return _set_enabled(name, True)


def disable(name: str) -> dict:
    """Disable a model entry."""
    return _set_enabled(name, False)


def _set_enabled(name: str, value: bool) -> dict:
    if not valid_name(name):
        raise ValueError(f"invalid model name: {name!r}")
    data = _load()
    e = _entry(data["entries"], name)
    if e is None:
        raise FileNotFoundError(f"model not found: {name}")
    e["enabled"] = value
    _save(data)
    return {"ok": True, "name": name, "enabled": value}


# ── per-harness assignment ────────────────────────────────────────────────
def assign(harness: str, name: str) -> dict:
    """Select the model used by one harness (hermes | claude | atropos)."""
    if harness not in HARNESSES:
        raise ValueError(f"harness must be one of: {', '.join(HARNESSES)}")
    if not valid_name(name):
        raise ValueError(f"invalid model name: {name!r}")
    data = _load()
    e = _entry(data["entries"], name)
    if e is None:
        raise FileNotFoundError(f"model not found: {name}")
    data["assignments"][harness] = name
    _save(data)
    return {"ok": True, "harness": harness, "name": name}


def assignments() -> dict:
    """Current harness → model name map."""
    return deepcopy(_load()["assignments"])


def active(harness: str) -> dict | None:
    """Resolve the model a harness should use — or None.

    Resolution order: per-harness assignment → the first *enabled* default
    seed entry → router.get()'s current model. Returns a router.get()-shaped
    dict ``{model, base_url, api_key_env}`` (plus name/provider) or None
    when nothing matches.
    """
    if harness not in HARNESSES:
        return None
    data = _load()
    name = data["assignments"].get(harness)
    e = _entry(data["entries"], name) if name else None
    if e is None or not e.get("enabled"):
        for candidate in data["entries"]:
            if candidate.get("enabled"):
                e = candidate
                break
        else:
            e = None
    if e is None:
        return None
    return {
        "name": e["name"],
        "provider": e.get("provider", ""),
        "model": e.get("model") or e["name"],
        "base_url": e.get("base_url") or "",
        "api_key_env": e.get("api_key_env") or "",
    }


def toggle(name: str) -> dict:
    """Toggle enable/disable for a model entry."""
    if not valid_name(name):
        raise ValueError(f"invalid model name: {name!r}")
    data = _load()
    e = _entry(data["entries"], name)
    if e is None:
        raise FileNotFoundError(f"model not found: {name}")
    e["enabled"] = not e.get("enabled", True)
    _save(data)
    return {"ok": True, "name": name, "enabled": e["enabled"]}


def list_providers() -> list:
    """Known providers from router.ROUTERS."""
    out = []
    for pname, info in router.ROUTERS.items():
        out.append({
            "name": pname,
            "description": info.get("description", ""),
            "base_url": info.get("base_url", ""),
            "api_key_env": info.get("api_key_env", ""),
            "model": info.get("model", ""),
            "model_kinds": info.get("model_kinds", ["chat"]),
        })
    return out


def test_provider(name: str) -> dict:
    """Ping a provider's /v1/models endpoint."""
    res = router.discover_models(name, timeout=10)
    return {"ok": res.get("ok", False), "models": res.get("models", []),
            "count": res.get("count", 0), "error": res.get("error")}


if __name__ == "__main__":
    for h in HARNESSES:
        resolved = active(h)
        if resolved:
            print(f"  {h}: {resolved['name']} ({resolved['model']} @ {resolved['base_url'] or 'default'})")
        else:
            print(f"  {h}: (none)")
