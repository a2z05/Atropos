#!/usr/bin/env python3
"""Atropos router — the model backends: nain / omni / local.

Routers are the sockets Atropos talks to:

  nain   → 9Router     (local/remote AI gateway, OpenAI-compatible REST,
                        ``$NINEROUTER_URL``; auto-fallback, 695+ models)
  omni   → OmniRoute   (smart AI router with auto-fallback + skills/memory,
                        ``$OMNIROUTE_BASE_URL`` / ``omniroute`` CLI)
  local  → Ollama      (localhost:11434 openai-compatible)

The three identifiers stay nain/omni/local — they are *slots*; what they
point at is now real, live, manageable gateways. ``deepmo`` is a model
served by 9Router, never a router.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, detect

ROUTERS = {
    "nain": {
        "api_key_env": "NINEROUTER_KEY",
        "model": "deepmo",
        "base_url": "",
        "description": "Nain — the 9Router OpenAI-compatible gateway (deepmo)",
        "env_keys": ["NINEROUTER_URL", "NINEROUTER_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY"],
        "model_kinds": ["chat", "tts", "image", "embed"],
    },
    "omni": {
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o",
        "base_url": "https://openrouter.ai/api/v1",
        "description": "OmniRoute — multi-provider gateway (OpenRouter protocol)",
        "env_keys": ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
        "model_kinds": ["chat", "tts", "image", "embed"],
    },
    "local": {
        "api_key_env": "OLLAMA_HOST",
        "model": "llama3",
        "base_url": "http://localhost:11434/v1",
        "description": "Local Ollama",
        "env_keys": ["OPENAI_BASE_URL"],
        "model_kinds": ["chat", "embed"],
    },
}


def get():
    cfg = config.load()
    return cfg.get("router", {})


def available():
    return list(ROUTERS.keys())


def info(name: str) -> dict:
    """Resolved runtime info for a router: backend, url, model, key-set."""
    if name not in ROUTERS:
        return {"error": f"unknown router: {name}"}
    r = ROUTERS[name]
    key = os.environ.get(r["api_key_env"], "")
    url = r["base_url"]
    if name == "nain" and os.environ.get("NINEROUTER_URL"):
        url = os.environ["NINEROUTER_URL"].rstrip("/")
    if name == "omni" and os.environ.get("OMNIROUTE_BASE_URL"):
        url = os.environ["OMNIROUTE_BASE_URL"].rstrip("/")
    return {"name": name, "backend": r.get("backend", name), "base_url": url,
            "model": r["model"], "description": r["description"],
            "api_key_set": bool(key), "model_kinds": r.get("model_kinds", ["chat"])}


def omniroute(cmd: list) -> dict:
    """Run the omniroute CLI (manage: memory, skills, oauth, api, configure…)."""
    try:
        proc = subprocess.run(["omniroute"] + cmd, capture_output=True, text=True, timeout=90)
        return {"ok": proc.returncode == 0, "output": proc.stdout,
                "error": proc.stderr.strip() or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_active(name: str):
    """Activate a router and persist it (records a manual failover hold)."""
    if name not in ROUTERS:
        raise ValueError(f"unknown router: {name}. Available: {available()}")
    rinfo = ROUTERS[name]
    cfg = config.load()
    cfg["router"] = {
        "active": name,
        "base_url": rinfo["base_url"],
        "api_key_env": rinfo["api_key_env"],
        "model": rinfo["model"],
    }
    config.save(cfg)
    # a manual choice is authoritative: failover holds off for its grace period
    try:
        from .failover import mark_manual
        mark_manual(name)
    except Exception:
        pass
    return cfg["router"]


def apply_to_hermes():
    """Write router config to hermes-agent env/config if detected."""
    r = get()
    hermes = detect.hermes_home()
    if not hermes:
        return False, "hermes_home not found"
    env_file = hermes / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip().strip('"').strip("'")
    if r.get("base_url"):
        existing["OPENAI_BASE_URL"] = r["base_url"]
    elif "OPENAI_BASE_URL" in existing:
        del existing["OPENAI_BASE_URL"]
    if r.get("model"):
        existing["DEFAULT_MODEL"] = r["model"]
    lines = [f"{k}={v}" for k, v in existing.items() if v]
    env_file.write_text("\n".join(lines) + "\n")
    return True, f"wrote {env_file}"


def apply_to_claude():
    """Write to ~/.claude/settings.json if present."""
    claude_dir = detect._home() / ".claude"
    if not claude_dir.exists():
        return False, "~/.claude not found"
    settings_file = claude_dir / "settings.json"
    r = get()
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except Exception:
            pass
    settings["router"] = r.get("active", "nain")
    if r.get("model"):
        settings["default_model"] = r["model"]
    settings_file.write_text(json.dumps(settings, indent=2) + "\n")
    return True, f"wrote {settings_file}"


def apply_all():
    results = []
    results.append(("hermes", *apply_to_hermes()))
    results.append(("claude", *apply_to_claude()))
    return results


def ping(name: str, timeout: float = 8.0):
    """Ping a router with a tiny completion request.

    Returns dict with keys: ok, latency_ms, model, error.
    """
    if name not in ROUTERS:
        return {"ok": False, "error": f"unknown router: {name}", "latency_ms": None}
    rinfo = ROUTERS[name]
    model = rinfo["model"]
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(_endpoint(name, "chat/completions"),
                                     data=payload, headers=_headers(name), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            latency = round((time.monotonic() - t0) * 1000)
            try:
                data = json.loads(body)
                returned_model = data.get("model", model)
            except Exception:
                returned_model = model
            return {"ok": True, "latency_ms": latency, "model": returned_model, "error": None}
    except urllib.error.HTTPError as e:
        latency = round((time.monotonic() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "model": model,
                "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        latency = round((time.monotonic() - t0) * 1000)
        return {"ok": False, "latency_ms": latency, "model": model, "error": str(e)}


def _endpoint(name: str, path: str = "chat/completions") -> str:
    """Resolve the base URL for a router and append an API path."""
    rinfo = ROUTERS[name]
    base = rinfo["base_url"]
    if name == "local" and os.environ.get("OLLAMA_HOST"):
        host = os.environ["OLLAMA_HOST"].rstrip("/")
        base = host if host.startswith("http") else "http://" + host
    elif name == "nain" and os.environ.get("NINEROUTER_URL"):
        base = os.environ["NINEROUTER_URL"].rstrip("/")
    elif not base and os.environ.get("OPENAI_BASE_URL"):
        base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    elif not base:
        base = "https://api.openai.com/v1"
    return base.rstrip("/") + "/" + path.lstrip("/")


def _headers(name: str) -> dict:
    rinfo = ROUTERS[name]
    key = os.environ.get(rinfo["api_key_env"], "")
    headers = {"Content-Type": "application/json"}
    if key and rinfo["api_key_env"] != "OLLAMA_HOST":
        headers["Authorization"] = f"Bearer {key}"
    elif name == "nain" and not key:
        # 9Router serves its model catalog to the public key too; chat needs
        # NINEROUTER_KEY (set it in the shell env: export NINEROUTER_KEY=...).
        headers["Authorization"] = "Bearer public"
    return headers


def discover_models(name: str, timeout: float = 8.0) -> dict:
    """GET /v1/models for a router. Returns {ok, models: [...], error}."""
    if name not in ROUTERS:
        return {"ok": False, "error": f"unknown router: {name}"}
    try:
        req = urllib.request.Request(_endpoint(name, "models"), headers=_headers(name))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
        return {"ok": True, "models": ids[:100], "count": len(ids), "error": None}
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}


def health() -> dict:
    """Overall router health: active, all pings, model kinds available."""
    active = get().get("active", "nain")
    pings = {name: ping(name) for name in ROUTERS}
    return {
        "active": active,
        "pings": pings,
        "all_down": all(not p["ok"] for p in pings.values()),
        "kinds": ROUTERS.get(active, {}).get("model_kinds", ["chat"]),
        "models": {name: discover_models(name).get("count", 0) for name in ROUTERS},
    }


def chat(name: str, message: str, model: str | None = None, timeout: float = 60.0) -> dict:
    """Send one chat message through a router. Returns {ok, text, model, error}."""
    if name not in ROUTERS:
        return {"ok": False, "error": f"unknown router: {name}"}
    rinfo = ROUTERS[name]
    payload = json.dumps({
        "model": model or rinfo["model"],
        "messages": [{"role": "user", "content": message}],
        "stream": False,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(_endpoint(name, "chat/completions"),
                                     data=payload, headers=_headers(name), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
        return {"ok": True,
                "text": (data.get("choices") or [{}])[0].get("message", {}).get("content", ""),
                "model": data.get("model", model or rinfo["model"]),
                "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    r = get()
    print(f"active: {r.get('active', '?')}")
    print(f"model:  {r.get('model', '?')}")
    print(f"url:    {r.get('base_url', 'default') or 'default'}")
    print(f"available: {', '.join(available())}")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        for name in available():
            result = ping(name)
            icon = "OK" if result["ok"] else "FAIL"
            lat = f'{result["latency_ms"]}ms' if result["latency_ms"] is not None else "—"
            print(f"  [{icon}] {name}: {lat} — {result.get('error') or result.get('model', '')}")
