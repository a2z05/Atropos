#!/usr/bin/env python3
"""Atropos router — single control for Hermes + Claude shared router config.

Routers are: nain (main, serves deepmo model), omni (OpenRouter),
local (Ollama). deepmo is a MODEL served by nain, not a router.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, detect

ROUTERS = {
    "nain": {
        "api_key_env": "OPENAI_API_KEY",
        "model": "deepmo",
        "base_url": "",
        "description": "Nain router (serves deepmo model)",
        "env_keys": ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
    },
    "omni": {
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o",
        "base_url": "https://openrouter.ai/api/v1",
        "description": "OmniRouter (OpenRouter/GPT-4o)",
        "env_keys": ["OPENAI_BASE_URL", "OPENAI_API_KEY"],
    },
    "local": {
        "api_key_env": "OLLAMA_HOST",
        "model": "llama3",
        "base_url": "http://localhost:11434/v1",
        "description": "Local Ollama",
        "env_keys": ["OPENAI_BASE_URL"],
    },
}


def get():
    cfg = config.load()
    return cfg.get("router", {})


def available():
    return list(ROUTERS.keys())


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
    base = rinfo["base_url"]
    model = rinfo["model"]
    # Build endpoint URL
    if name == "local" and os.environ.get("OLLAMA_HOST"):
        # Ollama host env wins: could be host:port or full URL
        host = os.environ["OLLAMA_HOST"].rstrip("/")
        endpoint = (host if host.startswith("http") else "http://" + host) + "/v1/chat/completions"
    elif base:
        endpoint = base.rstrip("/") + "/chat/completions"
    else:
        # nain: use hermes env OPENAI_BASE_URL or OpenAI default
        env_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        endpoint = env_url.rstrip("/") + "/chat/completions"
    # Build auth header (Ollama needs none; a bare host var is not a key)
    api_key_env = rinfo["api_key_env"]
    api_key = os.environ.get(api_key_env, "")
    headers = {"Content-Type": "application/json"}
    if api_key and api_key_env != "OLLAMA_HOST":
        headers["Authorization"] = f"Bearer {api_key}"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            latency = round((time.monotonic() - t0) * 1000)
            # Parse response to confirm model
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
