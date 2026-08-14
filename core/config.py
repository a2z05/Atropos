#!/usr/bin/env python3
"""Atropos config — minimal YAML (subset) parser/writer, stdlib only.

Config lives at ~/.atropos/config.yaml. Fallbacks: env vars. Nothing
hardcoded — all paths come from detect.py or this file.

The parser is a pragmatic YAML *subset* tuned for Atropos's own config and
the hacks/*.yml files:

  * mappings  ``key: value`` (nested by 2-space indentation)
  * sequences ``- item`` (block form) and ``[a, b]`` (flow form)
  * scalars   quoted strings, booleans, null, ints, floats
  * comments  ``#`` to end of line
  * block scalars  ``|``  keep exactly one trailing newline
                   ``|-`` strip all trailing newlines
                   ``|+`` keep all trailing newlines
  * blank lines inside blocks are preserved

Block-scalar convention (documented, project-specific): every body line is
stripped of exactly *base* = (indentation of the line holding the ``|``
indicator) + 2 spaces. Deeper indentation inside the body is preserved
verbatim. This lets us store Python source anchors — which always begin
with leading whitespace — exactly as they appear in the target file, which
is required for the patches engine's exact-string replacement.

``dump_yaml`` emits the same convention, so config round-trips cleanly.
"""
import json
import os
import re
import sys
from pathlib import Path

from . import detect

DEFAULTS = {
    "router": {
        "active": "nain",
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
        "alias": "nain",
    },
    "dashboard": {
        "port": 8787,
        "host": "127.0.0.1",
    },
    "guest": {
        "enabled": False,
        "persona_path": "",  # empty → hermes_home()/assets/guest_persona.md
    },
    "version": "1.0.0",
}


def _indent_of(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _content(raw: str) -> str:
    """Line without leading spaces."""
    return raw.lstrip(" ")


# ── minimal YAML subset parser ────────────────────────────────────────────
# ``|``/``|-``/``|+`` literal block scalars.
BLOCK_SCALAR_RE = re.compile(r"^(\|[-+]?)\s*(#.*)?$")


def _parse_block(lines, idx=0, indent=0):
    """Parse a mapping block. Returns (dict, next_index)."""
    result = {}
    i = idx
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        cur_indent = _indent_of(raw)
        if cur_indent < indent:
            break
        if cur_indent > indent:
            i += 1  # malformed (parent already consumed a sub-block)
            continue
        line = _content(raw)
        # list item at block level -> parent handles it; bail here
        if line.startswith("- "):
            break
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip().strip('"').strip("'")
        val = val.strip()

        # inline block-scalar indicator: "old: |-"
        bm = BLOCK_SCALAR_RE.match(val)
        if bm:
            block, ni = _parse_block_scalar(lines, i + 1, cur_indent, bm.group(1))
            result[key] = block
            i = ni
            continue

        if val.startswith("#") or val == "":
            nxt = _peek_next(lines, i + 1)
            if nxt is not None:
                nxt_idx = i + 1 + nxt[1]
                nxt_indent = _indent_of(lines[nxt_idx])
                if nxt_indent > cur_indent:
                    # block scalar opener on its own line
                    if _looks_like_block_scalar(nxt[0]):
                        indicator = BLOCK_SCALAR_RE.match(nxt[0]).group(1)
                        block, ni = _parse_block_scalar(lines, nxt_idx + 1,
                                                        nxt_indent, indicator)
                        result[key] = block
                        i = ni
                        continue
                    if nxt[0].startswith("- "):
                        sub, ni = _parse_list(lines, i + 1, nxt_indent)
                    else:
                        sub, ni = _parse_block(lines, i + 1, nxt_indent)
                    result[key] = sub
                    i = ni
                    continue
            result[key] = ""
            i += 1
            continue

        # inline flow list: [a, b]
        if val.startswith("["):
            if val.endswith("]"):
                result[key] = [_scalar(v) for v in val.strip("[]").split(",") if v.strip()]
                i += 1
                continue
            # multiline flow list: accumulate until ]
            items, buf = [], val[1:]
            i += 1
            while i < len(lines):
                cur = _content(lines[i]).rstrip()
                if "]" in cur:
                    buf += " " + cur.split("]")[0]
                    i += 1
                    break
                buf += " " + cur
                i += 1
            result[key] = [_scalar(v) for v in buf.split(",") if v.strip()]
            continue

        result[key] = _scalar(val)
        i += 1
    return result, i


def _peek_next(lines, i):
    """Return (content, offset) of next non-blank/non-comment line, or None."""
    offset = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            offset += 1
            continue
        return _content(raw), offset
    return None


def _looks_like_block_scalar(content: str) -> bool:
    return bool(BLOCK_SCALAR_RE.match(content))


def _parse_block_scalar(lines, start, opener_indent, indicator):
    """Parse a literal block scalar body starting at ``lines[start]``.

    The body is dedented by the minimum indentation found across its
    non-empty lines, so deeper anchor indentation is preserved and shallow
    bodies keep no stray leading spaces. The block ends at the first
    non-blank line dedented back to (or above) the opener.
    """
    # Determine the common indentation to strip. Real-YAML semantics: the
    # body is dedented by the minimum indentation across its non-empty
    # lines (relative to the opener), so deeper anchor indentation survives
    # and shallow bodies aren't left with stray leading spaces.
    body = []
    k = start
    while k < len(lines):
        raw = lines[k]
        if not raw.strip():
            body.append("")
            k += 1
            continue
        if _indent_of(raw) <= opener_indent:
            break
        body.append(raw)
        k += 1
    non_empty = [l for l in body if l.strip()]
    base = min(_indent_of(l) for l in non_empty) if non_empty else opener_indent + 2
    body_lines = [l[base:] if len(l) >= base else "" for l in body]

    if indicator == "|+":
        body = "\n".join(body_lines)
        if body_lines:
            body += "\n"
    else:
        while body_lines and body_lines[-1] == "":
            body_lines.pop()
        if indicator == "|":
            body = "\n".join(body_lines) + ("\n" if body_lines else "")
        else:  # |-
            body = "\n".join(body_lines)
    return body, k


def _parse_list(lines, idx=0, indent=0):
    """Parse a block list at the given indentation. Returns (list, next_index)."""
    items = []
    i = idx
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        cur_indent = _indent_of(raw)
        if cur_indent < indent:
            break
        if cur_indent > indent:
            i += 1
            continue
        line = _content(raw)
        if not line.startswith("- "):
            break
        rest = line[2:].strip()
        if rest.startswith("[") and rest.endswith("]"):
            items.append([_scalar(v) for v in rest.strip("[]").split(",") if v.strip()])
            i += 1
            continue
        # nested mapping: "- key: value"
        if ":" in rest and not rest.startswith(("'", '"')):
            sub_lines = ["  " * cur_indent + rest] + list(lines[i + 1:])
            sub, ni = _parse_block(sub_lines, 0, cur_indent + 2)
            items.append(sub)
            i += ni
            continue
        items.append(_scalar(rest))
        i += 1
    return items, i


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
    data, _ = parse_lines(lines)
    return data


def parse_lines(lines) -> tuple:
    """Parse a list of lines. Returns (data, next_index). Used by tests."""
    if not lines:
        return {}, 0
    if _content(lines[0]).startswith("- "):
        return _parse_list(lines, 0, 0)
    return _parse_block(lines, 0, 0)


# ── YAML writer ───────────────────────────────────────────────────────────
def dump_yaml(obj: dict, indent=0) -> str:
    """Serialize dict (scalars, nested dicts, lists, multi-line strings)."""
    out = []
    pad = " " * indent
    if isinstance(obj, list):
        for item in obj:
            out.append(f"{pad}- {_dump_scalar(item)}")
        return "\n".join(out)
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append(f"{pad}{k}:")
            out.append(dump_yaml(v, indent + 2))
        elif isinstance(v, list):
            out.append(f"{pad}{k}:")
            for item in v:
                out.append(f"{pad}  - {_dump_scalar(item)}")
        elif isinstance(v, bool):
            out.append(f"{pad}{k}: {'true' if v else 'false'}")
        elif v is None:
            out.append(f"{pad}{k}: null")
        elif isinstance(v, str) and ("\n" in v or v != v.strip()):
            # multi-line / leading-space strings must round-trip exactly.
            # Body lines are emitted with 2 extra spaces (base); the parser
            # strips base back off, so anchor indentation is preserved.
            out.append(f"{pad}{k}: |-")
            for line in v.rstrip("\n").split("\n"):
                out.append(f"{pad}  {line}")
        else:
            out.append(f"{pad}{k}: {_quote(str(v))}")
    return "\n".join(out)


def _dump_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    return _quote(str(v))


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
            print(f"[config] warning: failed to parse {p}: {e}", file=sys.stderr)
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
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        print(dump_yaml(load()))
    else:
        print(config_path())