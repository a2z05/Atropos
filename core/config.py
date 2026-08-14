#!/usr/bin/env python3
"""Atropos config — minimal YAML (subset) parser/writer, stdlib only.

Config lives at ~/.atropos/config.yaml. Fallbacks: env vars. Nothing
hardcoded — all paths come from detect.py or this file.
"""
import json
import os
import re
from pathlib import Path

from . import detect

DEFAULTS = {
    "router": {
        "active": "deepmo",
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY",
        "model": "deepmo",
    },
    "hermes": {
        "home": "",          # filled from detect on load
        "log_channel": "",   # filled from env TELEGRAM_LOG_CHANNEL if set
    },
    "claude": {
        "model": "sonnet",
        "alias": "deepmo",
    },
    "dashboard": {
        "port": 8787,
        "host": "127.0.0.1",
    },
}


# ── minimal YAML subset parser ────────────────────────────────────────────
# Supports: key: value, nested 2-space blocks, lists ("- item"), quotes,
# comments (#), blank lines. Enough for our config + hacks files.
def _parse_block(lines, idx=0, indent=0):
    result = {}
    i = idx
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        cur_indent = len(raw) - len(raw.lstrip(" "))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            # should not happen; skip malformed
            i += 1
            continue
        line = raw.strip()
        # list item at block level -> parent handles it; bail here
        if line.startswith("- "):
            break
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip().strip('"').strip("'")
        val = val.strip()
        if val.startswith("#") or val == "":
            # nested block or empty
            if i + 1 < len(lines) and _next_indented(lines, i + 1):
                sub, ni = _parse_block(lines, i + 1, cur_indent + 2)
                result[key] = sub
                i = ni
                continue
            result[key] = ""
            i += 1
            continue
        # list value: "- a, - b" on same line or following
        if val.startswith("["):
            val = val.strip("[]")
            result[key] = [v.strip() for v in val.split(",") if v.strip()]
            i += 1
            continue
        val = _scalar(val)
        result[key] = val
        i += 1
    return result, i


def _next_indented(lines, i):
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        return len(raw) - len(raw.lstrip(" ")) > 0
    return False


def _scalar(val: str):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    if val.lower() in ("true", "yes", "on"):
        return True
    if val.lower() in ("false", "no", "off"):
        return False
    if val.lower() in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d+\.\d+", val):
        return float(val)
    return val


def parse_yaml(text: str) -> dict:
    lines = text.splitlines()
    data, _ = _parse_block(lines)
    return data


def dump_yaml(obj: dict, indent=0) -> str:
    """Serialize dict (scalars, nested dicts, lists) to our YAML subset."""
    out = []
    pad = " " * indent
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append(f"{pad}{k}:")
            out.append(dump_yaml(v, indent + 2))
        elif isinstance(v, list):
            out.append(f"{pad}{k}:")
            for item in v:
                out.append(f"{pad}  - {_quote(str(item))}")
        elif isinstance(v, bool):
            out.append(f"{pad}{k}: {'true' if v else 'false'}")
        elif v is None:
            out.append(f"{pad}{k}: null")
        else:
            out.append(f"{pad}{k}: {_quote(str(v))}")
    return "\n".join(out)


def _quote(s: str) -> str:
    if s and s[0] in "\"'":
        return s
    if any(c in s for c in ":#\n{}"):
        return json.dumps(s, ensure_ascii=False)
    return s


# ── config load/save ──────────────────────────────────────────────────────
def config_path() -> Path:
    return detect.atropos_home() / "config.yaml"


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    # env overrides
    cfg["hermes"]["home"] = str(detect.hermes_home())
    ch = os.environ.get("TELEGRAM_LOG_CHANNEL") or os.environ.get("ATRA_LOG_CHANNEL")
    if ch:
        cfg["hermes"]["log_channel"] = ch
    p = config_path()
    if p.exists():
        try:
            user = parse_yaml(p.read_text(encoding="utf-8"))
            _deep_merge(cfg, user)
        except Exception as e:
            # corrupt config: keep defaults, log to stderr
            print(f"[config] warning: failed to parse {p}: {e}", file=os.sys.stderr)
    return cfg


def _deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save(cfg: dict):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_yaml(cfg) + "\n", encoding="utf-8")


def get(key: str, default=None):
    """Dot-path getter: get('router.active')"""
    cfg = load()
    node = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(key: str, value):
    """Dot-path setter: set_path('router.active', 'omni')"""
    cfg = load()
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    save(cfg)
    return cfg


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        print(dump_yaml(load()))
    else:
        print(config_path())
