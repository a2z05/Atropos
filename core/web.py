#!/usr/bin/env python3
"""Atropos web tools — Hermes web_tools port, stdlib only.

Ported from hermes-agent/tools/web_tools.py and
hermes-agent/tools/url_safety.py (see also
hermes-agent/plugins/web/{searxng,brave_free,tavily,exa,parallel,firecrawl}/provider.py).

Keep the exact Hermes behavior in three places:

1. URL normalization + SSRF safety: ``normalize_url_for_request`` and the
   ``is_safe_url`` policy from url_safety.py — private/loopback/link-local
   IPs, CGNAT 100.64.0.0/10, and the always-blocked cloud-metadata floor
   (169.254.169.254, metadata.google.internal, ECS/IMDS endpoints) fail
   closed. Credential-bearing query params (token, api_key, …) block the
   call before any backend is touched (web_tools.web_extract_tool).
2. Result normalization: every provider maps to the canonical
   ``{"success": True, "data": {"web": [{"title","url","description",
   "position"}]}}`` shape (SearXNG / Brave / Tavily / Exa / Firecrawl
   provider files).
3. Extract post-processing: ``convert_base64_images_to_links`` (token-bomb
   guard) and ``_truncate_with_footer`` — pages over the char budget are
   head+tail cut with a footer, the full text stored under
   ``<home>/cache/web/`` (web_tools._store_full_text).

Deliberate deviations (stdlib):
- httpx -> urllib.request. Retry-with-backoff for 5xx/transient errors is
  ported from x_search_tool.py's loop and applied to every provider.
- The duckduckgo ``ddgs`` package provider is dropped (requires pip); the
  SearXNG backend can be used instead when a local instance is available.
- The Hermes config.yaml/web registry layer is replaced by env-var+settings
  detection: search backends are tried in the same priority order as
  ``_get_backend()`` (tavily, exa, parallel, firecrawl, searxng (5xx
  tolerant), brave-free), excluding pieces that need non-stdlib packages.
- Hermes' UA policy is kept: urllib's default python-urllib UA is spoofed
  with a Chrome 120 Mozilla UA (vision_tools.py UA spoofing pattern).
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

from . import detect, settings

DEFAULT_EXTRACT_CHAR_LIMIT = 15000        # web_tools.DEFAULT_EXTRACT_CHAR_LIMIT
MAX_STORED_TEXT_CHARS = 2_000_000         # web_tools.MAX_STORED_TEXT_CHARS
_REQUEST_TIMEOUT = 15
_EXTRACT_TIMEOUT = 30
_RETRIES = 2                              # x_search_tool.DEFAULT_X_SEARCH_RETRIES
_BASE64_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)                                        # vision_tools.py UA spoofing (same string)

# ── URL safety (ported from url_safety.py) ─────────────────────────────────

_PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                   "ALL_PROXY", "all_proxy")

_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})

_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("169.254.169.253"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
)
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

_SENSITIVE_QUERY_PARAM_NAMES = frozenset({
    "access_token", "api_key", "apikey", "auth_token", "authorization",
    "awsaccesskeyid", "client_secret", "credential", "credentials", "jwt",
    "password", "passwd", "secret", "session_id", "signature", "token",
    "x_amz_security_token", "x_amz_signature", "x-amz-security-token",
    "x-amz-signature",
})

_LEGACY_WEB_BACKENDS_PRIORITY = (
    ("tavily", "TAVILY_API_KEY", "search"),
    ("exa", "EXA_API_KEY", "search"),
    ("parallel", "PARALLEL_API_KEY", "search"),
    ("firecrawl", "FIRECRAWL_API_KEY", "search"),
    ("searxng", "SEARXNG_URL", "search"),
    ("brave-free", "BRAVE_SEARCH_API_KEY", "search"),
)


def _proxy_is_configured() -> bool:
    """Return True when at least one HTTP proxy env var is set."""
    return any(os.environ.get(v) for v in _PROXY_ENV_VARS)


def normalize_url_for_request(url: str) -> str:
    """Return an ASCII-safe HTTP URL for own URL tools (url_safety.py)."""
    if not isinstance(url, str):
        return url
    raw = url.strip()
    if not raw:
        return raw
    raw = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)\s+", r"\1", raw)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.scheme.lower() not in {"http", "https"}:
        return raw
    netloc = parsed.netloc
    hostname = parsed.hostname
    if hostname:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = hostname
        if ascii_host != hostname:
            netloc = netloc.replace(hostname, ascii_host, 1)
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?")
    fragment = quote(parsed.fragment, safe="/%:@!$&'()*+,;=?")
    return urlunsplit((parsed.scheme, netloc, path, query, fragment))


def sensitive_query_param_name(url: str):
    """Return the first credential-bearing query param name, or None."""
    if not isinstance(url, str) or "?" not in url:
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.query:
        return None
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if value and unquote(key).lower() in _SENSITIVE_QUERY_PARAM_NAMES:
            return key
    return None


# Credential-prefix alternation, ported from agent/redact.py (_PREFIX_PATTERNS
# + _PREFIX_RE): recognizable vendor key shapes embedded in a URL. The web
# extract tool checks url, unquote(url), normalized url, and unquote(normalized
# url) against this before any backend is called.
_SECRET_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join([
        r"sk-[A-Za-z0-9_-]{10,}",
        r"ghp_[A-Za-z0-9]{10,}",
        r"github_pat_[A-Za-z0-9_]{10,}",
        r"gho_[A-Za-z0-9]{10,}",
        r"ghu_[A-Za-z0-9]{10,}",
        r"ghs_[A-Za-z0-9]{10,}",
        r"ghr_[A-Za-z0-9]{10,}",
        r"xapp-\d+-[A-Za-z0-9-]{10,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"AIza[A-Za-z0-9_-]{30,}",
        r"pplx-[A-Za-z0-9]{10,}",
        r"fal_[A-Za-z0-9_-]{10,}",
        r"fc-[A-Za-z0-9]{10,}",
        r"bb_live_[A-Za-z0-9_-]{10,}",
        r"gAAAA[A-Za-z0-9_=-]{20,}",
        r"AKIA[A-Z0-9]{16}",
        r"sk_live_[A-Za-z0-9]{10,}",
        r"sk_test_[A-Za-z0-9]{10,}",
        r"rk_live_[A-Za-z0-9]{10,}",
        r"SG\.[A-Za-z0-9_-]{10,}",
        r"hf_[A-Za-z0-9]{10,}",
        r"r8_[A-Za-z0-9]{10,}",
        r"npm_[A-Za-z0-9]{10,}",
        r"pypi-[A-Za-z0-9_-]{10,}",
        r"dop_v1_[A-Za-z0-9]{10,}",
        r"doo_v1_[A-Za-z0-9]{10,}",
        r"am_[A-Za-z0-9_-]{10,}",
        r"sk_[A-Za-z0-9_]{10,}",
        r"tvly-[A-Za-z0-9]{10,}",
        r"exa_[A-Za-z0-9]{10,}",
        r"gsk_[A-Za-z0-9]{10,}",
        r"syt_[A-Za-z0-9]{10,}",
        r"retaindb_[A-Za-z0-9]{10,}",
        r"hsk-[A-Za-z0-9]{10,}",
        r"mem0_[A-Za-z0-9]{10,}",
        r"brv_[A-Za-z0-9]{10,}",
        r"xai-[A-Za-z0-9]{30,}",
        r"ntn_[A-Za-z0-9]{10,}",
        r"fw-[A-Za-z0-9]{30,}",
        r"fw_[A-Za-z0-9]{30,}",
        r"fpk_[A-Za-z0-9]{30,}",
        r"glpat-[A-Za-z0-9_\-]{10,}",
        r"gloas-[A-Za-z0-9_\-]{10,}",
        r"gldt-[A-Za-z0-9_\-]{10,}",
        r"glrt-[A-Za-z0-9_.\-]{10,}",
        r"glrtr-[A-Za-z0-9_.\-]{10,}",
        r"glcbt-[A-Za-z0-9_\-]{10,}",
        r"glptt-[A-Za-z0-9_\-]{10,}",
        r"glft-[A-Za-z0-9_\-]{10,}",
        r"glimt-[A-Za-z0-9_\-]{10,}",
        r"glagent-[A-Za-z0-9_\-]{10,}",
        r"glsoat-[A-Za-z0-9_\-]{10,}",
        r"glffct-[A-Za-z0-9_\-]{10,}",
        r"glwt-[A-Za-z0-9_\-]{10,}",
        r"GR1348941[A-Za-z0-9_\-]{10,}",
    ]) + r")(?![A-Za-z0-9_-])",
)


def _is_blocked_ip(ip) -> bool:
    """Return True when the IP is private/loopback/link-local/CGNAT (SSRF)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        embedded = ip.ipv4_mapped
        return (embedded.is_private or embedded.is_loopback or
                embedded.is_link_local or embedded.is_reserved or
                embedded.is_multicast or embedded.is_unspecified or
                embedded in _CGNAT_NETWORK)
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


# Cached toggle: allow private-IP resolution when the user opts out.
_allow_private_resolved = False
_cached_allow_private = False


def _resolve_allow_private_urls() -> bool:
    """Resolve the private-URL toggle: env var, then settings."""
    env_val = os.getenv("ATROPOS_ALLOW_PRIVATE_URLS", "").strip().lower()
    if env_val in {"true", "1", "yes"}:
        return True
    if env_val in {"false", "0", "no"}:
        return False
    try:
        return bool(settings.get("web.allow_private_urls", False))
    except Exception:
        return False


def _global_allow_private_urls() -> bool:
    """Return True when the user has opted out of private-IP blocking."""
    global _allow_private_resolved, _cached_allow_private
    if _allow_private_resolved:
        return _cached_allow_private
    _allow_private_resolved = True
    _cached_allow_private = _resolve_allow_private_urls()
    return _cached_allow_private


def _reset_allow_private_cache() -> None:
    """Reset the cached toggle — only for tests (url_safety.py name)."""
    global _allow_private_resolved, _cached_allow_private
    _allow_private_resolved = False
    _cached_allow_private = False


def is_safe_url(url: str) -> bool:
    """Return True when the URL target is not a private/internal address.

    Fails closed on DNS errors. Cloud-metadata endpoints stay blocked even
    with the private-URL toggle on (url_safety.is_safe_url).
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            return False
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            return False
        allow_all_private = _global_allow_private_urls()
        try:
            addr_info = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            _is_literal_ip = True
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                _is_literal_ip = False
            # In sandbox/proxy environments DNS may be blocked; a configured
            # proxy then does the resolution, so allow the request through.
            if not _is_literal_ip and _proxy_is_configured():
                return True
            return False
        for _family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if "%" in ip_str:
                ip_str = ip_str.split("%")[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return False
            if ip in _ALWAYS_BLOCKED_IPS or any(
                ip in net for net in _ALWAYS_BLOCKED_NETWORKS
            ):
                return False
            if not allow_all_private and _is_blocked_ip(ip):
                return False
        return True
    except Exception:
        return False


# ── HTTP transport helpers (httpx -> urllib; retry from x_search_tool) ─────

def _ua() -> str:
    return _BASE64_UA


def _request(method: str, url: str, headers=None, data=None, json_body=None,
             timeout: float = _REQUEST_TIMEOUT, retries: int = _RETRIES):
    """Perform an HTTP request with the Hermes UA + retry-on-5xx/transient.

    Retry-with-backoff loop ported from x_search_tool.x_search_tool: 5xx
    and read-timeout/connection errors are retried up to ``retries`` extra
    attempts, sleeping min(5.0, 1.5 * (attempt + 1)) between tries.
    Returns the parsed JSON body (or raw bytes when not JSON).
    Raises ``urllib.error.HTTPError`` / ``URLError`` when exhausted.
    """
    if headers is None:
        headers = {}
    headers = dict(headers)
    headers.setdefault("User-Agent", _ua())
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = data if isinstance(data, bytes) else str(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    last_exc = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return json.loads(raw.decode("utf-8", "replace"))
                return raw
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt >= retries:
                raise
            last_exc = e
            time.sleep(min(5.0, 1.5 * (attempt + 1)))
        except (urllib.error.URLError, OSError) as e:
            if attempt >= retries:
                raise
            last_exc = e
            time.sleep(min(5.0, 1.5 * (attempt + 1)))
    raise last_exc if last_exc is not None else RuntimeError("request failed")


def _get_json(url: str, headers=None, timeout: float = _REQUEST_TIMEOUT, retries: int = _RETRIES):
    return _request("GET", url, headers=headers, timeout=timeout, retries=retries)


def _post_json(url: str, payload: dict, headers=None, timeout: float = _REQUEST_TIMEOUT, retries: int = _RETRIES):
    return _request("POST", url, headers=headers, json_body=payload, timeout=timeout, retries=retries)


def _search_fallback_chain(query: str, k: int):
    """Try each configured backend in priority order; return first success.

    Returns ``(results, backend)`` or ``(None, None)`` when every backend
    failed. The walk mirrors web_tools' registry behavior: the configured
    backends are tried in ``_get_backend`` priority order and the next
    available provider substitutes when the previous one errors
    (``get_active_search_provider`` fallback).
    """
    configured = (settings.get("web.backend", "") or "").lower().strip()
    backends = _available_backends()
    order = [b for b in backends if b == configured] + [b for b in backends if b != configured]
    for backend in order:
        resp = _provider_search(backend, query, k)
        if resp.get("success"):
            return resp.get("data", {}).get("web", []), backend
    return None, None


# ── Backend selection (web_tools._get_backend candidate order) ─────────────

def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _searxng_url() -> str:
    """Return SEARXNG_URL from env (searxng provider)."""
    return _env("SEARXNG_URL")


def _available_backends() -> list:
    """Return usable search backends in Hermes priority order."""
    out = []
    for backend, var, _cap in _LEGACY_WEB_BACKENDS_PRIORITY:
        if var in ("TAVILY_API_KEY", "EXA_API_KEY", "PARALLEL_API_KEY",
                   "BRAVE_SEARCH_API_KEY") and not _env(var):
            continue
        if backend == "firecrawl" and not (
            _env("FIRECRAWL_API_KEY") or _env("FIRECRAWL_API_URL")
        ):
            continue
        if backend == "searxng" and not _searxng_url():
            continue
        out.append(backend)
    return out


def _get_backend() -> str:
    """Return the configured backend, else the first available, else ''."""
    configured = (settings.get("web.backend", "") or "").lower().strip()
    if configured in _available_backends():
        return configured
    candidates = _available_backends()
    return candidates[0] if candidates else ""


def _provider_search(backend: str, query: str, limit: int) -> dict:
    """One provider's search; returns the canonical web_tools response dict."""
    url = None
    headers = None
    params = None
    payload = None
    if backend == "tavily":
        base = _env("TAVILY_BASE_URL") or "https://api.tavily.com"
        payload = {
            "query": query,
            "max_results": min(limit, 20),
            "include_raw_content": False,
            "include_images": False,
            "api_key": _env("TAVILY_API_KEY"),
        }
        url = f"{base}/search"
    elif backend == "exa":
        # Exa raw HTTP equivalent of exa_py: contents.highlights search.
        payload = {
            "query": query,
            "numResults": min(limit, 20),
            "contents": {"highlights": True},
        }
        url = "https://api.exa.ai/search"
        headers = {"x-api-key": _env("EXA_API_KEY")}
    elif backend == "parallel":
        base = _env("PARALLEL_BASE_URL") or "https://api.parallel.ai"
        payload = {"query": query, "limit": min(limit, 20)}
        url = f"{base}/v1/search"
        headers = {"Authorization": f"Bearer {_env('PARALLEL_API_KEY')}"}
    elif backend == "firecrawl":
        api_url = (_env("FIRECRAWL_API_URL") or "https://api.firecrawl.dev").rstrip("/")
        payload = {"query": query, "limit": min(limit, 20)}
        url = f"{api_url}/v1/search"
        if _env("FIRECRAWL_API_KEY"):
            headers = {"Authorization": f"Bearer {_env('FIRECRAWL_API_KEY')}"}
    elif backend == "searxng":
        params = "&".join(
            urllib.parse.urlencode(
                {"q": query, "format": "json", "pageno": 1}
            ).split("&")
        )
        url = f"{_searxng_url().rstrip('/')}/search?{params}"
    elif backend == "brave-free":
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": max(1, min(int(limit), 20))}
        )
        headers = {"X-Subscription-Token": _env("BRAVE_SEARCH_API_KEY"),
                   "Accept": "application/json"}
    else:
        return {"success": False, "error": f"Unknown backend: {backend}"}

    try:
        if payload is not None:
            data = _post_json(url, payload, headers=headers)
        else:
            data = _get_json(url, headers=headers)
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"{backend} returned HTTP {e.code}"}
    except Exception as e:
        return {"success": False, "error": f"Could not reach {backend}: {e}"}

    if not isinstance(data, dict):
        return {"success": False, "error": "Could not parse search response as JSON"}
    raw_results = data.get("results", [])
    if backend == "brave-free":
        raw_results = (data.get("web") or {}).get("results", []) or []
    return _normalize_search_results(raw_results, limit)


def _normalize_search_results(raw_results: list, limit: int) -> dict:
    """Map raw provider hits to the canonical {success, data: {web: [...]}}."""
    web_results = []
    for i, r in enumerate(raw_results[:limit]):
        web_results.append({
            "title": str(r.get("title", "")),
            "url": str(r.get("url", "") or r.get("href", "")),
            "description": str(r.get("content", "") or r.get("description", "")
                               or r.get("body", "")),
            "position": i + 1,
        })
    return {"success": True, "data": {"web": web_results}}


# ── Extract: base64 guard + truncate-and-store (web_tools.py) ──────────────

def convert_base64_images_to_links(text: str) -> str:
    """Replace inline base64 image blobs with labeled markdown placeholders.

    Base64 image payloads are token bombs; real http(s) image links are
    kept so the agent can still fetch them. Exact regexes from
    web_tools.convert_base64_images_to_links.
    """
    def _md_repl(m: "re.Match[str]") -> str:
        alt = (m.group("alt") or "").strip()
        return f"[IMAGE: {alt}]" if alt else "[IMAGE]"

    md_b64 = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)"
    )
    out = md_b64.sub(_md_repl, text)
    out = re.sub(r"\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)", "[IMAGE]", out)
    out = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[IMAGE]", out)
    return out


def _cache_dir() -> Path:
    d = detect.atropos_home() / "cache" / "web"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_full_text(url: str, content: str):
    """Write the full page to <home>/cache/web and return its absolute path.

    Best-effort (web_tools._store_full_text); the caller still receives the
    truncated content when storage fails.
    """
    try:
        host = (urlparse(url).hostname or "page").replace(":", "_")
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-") or "page"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        path = _cache_dir() / f"{slug}-{digest}.md"
        if len(content) > MAX_STORED_TEXT_CHARS:
            content = (
                content[:MAX_STORED_TEXT_CHARS]
                + f"\n\n[... stored copy truncated at {MAX_STORED_TEXT_CHARS:,} chars "
                f"of {len(content):,}; re-extract a more specific URL for the rest ...]"
            )
        path.write_text(content, encoding="utf-8")
        return str(path)
    except Exception:
        return None


def _truncate_with_footer(content: str, url: str, char_limit: int):
    """Return (model_text, was_truncated) — web_tools._truncate_with_footer.

    Pages over the budget get a ~75/25 head+tail window snapped to markdown
    line boundaries, plus a footer naming the stored full-text path and the
    read_file call that pages through the omitted middle.
    """
    if len(content) <= char_limit:
        return content, False
    head_budget = int(char_limit * 0.75)
    tail_budget = char_limit - head_budget
    head = content[:head_budget]
    tail = content[-tail_budget:]
    nl = head.rfind("\n")
    if nl > head_budget * 0.5:
        head = head[:nl]
    nl = tail.find("\n")
    if 0 <= nl < tail_budget * 0.5:
        tail = tail[nl + 1:]
    total = len(content)
    stored_path = _store_full_text(url, content)
    footer_lines = [
        "",
        "─" * 8 + " [TRUNCATED] " + "─" * 8,
        f"Showing {len(head):,} chars (head) + {len(tail):,} chars (tail) "
        f"of {total:,} total clean characters.",
    ]
    if stored_path:
        middle_start_line = head.count("\n") + 2
        footer_lines.append(f"Full text saved to: {stored_path}")
        footer_lines.append(
            f'To read the omitted middle: read_file path="{stored_path}" '
            f"offset={middle_start_line} limit=200  (the file is the complete page; "
            f"raise/lower offset to page through it)."
        )
    else:
        footer_lines.append(
            "Full text could not be stored; re-run web_extract on a more "
            "specific URL or use a local browser for the complete page."
        )
    footer_lines.append("─" * 29)
    model_text = head + "\n\n[... middle omitted — see footer ...]\n\n" + tail
    model_text += "\n" + "\n".join(footer_lines)
    return model_text, True


def _get_extract_char_limit() -> int:
    """Resolve the per-page char budget from settings, clamped 2k..500k."""
    try:
        configured = settings.get("web.extract_char_limit")
        if configured is not None:
            return max(2000, min(int(configured), 500_000))
    except (TypeError, ValueError):
        pass
    return DEFAULT_EXTRACT_CHAR_LIMIT


# ── Extract providers (Tavily contract; Firecrawl scraping URL) ────────────

def _provider_extract(urls: list, backend: str, format: str = "markdown"):
    """Run one backend's extraction; returns a list of per-URL results.

    Per-URL failures become result items with an ``error`` field rather than
    raising — same contract as the tavily/exa/firecrawl provider files.
    """
    failed = lambda u, err: {"url": u, "title": "", "content": "", "error": err}  # noqa: E731
    if backend == "tavily":
        try:
            base = _env("TAVILY_BASE_URL") or "https://api.tavily.com"
            raw = _post_json(base + "/extract", {
                "urls": urls, "include_images": False,
                "api_key": _env("TAVILY_API_KEY"),
            }, timeout=60)
        except Exception as e:
            return [failed(u, f"Tavily extract failed: {e}") for u in urls]
        documents = []
        for result in raw.get("results", []):
            url = result.get("url", "")
            content = result.get("raw_content", "") or result.get("content", "")
            documents.append({"url": url, "title": result.get("title", ""),
                              "content": content, "raw_content": content,
                              "metadata": {"sourceURL": url}})
        for fail in raw.get("failed_results", []):
            documents.append(failed(fail.get("url", ""), fail.get("error", "extraction failed")))
        for fail_url in raw.get("failed_urls", []):
            documents.append(failed(str(fail_url), "extraction failed"))
        return documents
    # Firecrawl self-hosted/cloud scrape per URL, like the provider's loop:
    # 60s timeout, SSRF re-check on the post-redirect URL, markdown chosen
    # over html (firecrawl provider._firecrawl_... format handling).
    results = []
    api_url = (_env("FIRECRAWL_API_URL") or "https://api.firecrawl.dev").rstrip("/")
    for url in urls:
        try:
            if not is_safe_url(url):
                results.append(failed(url, "Blocked: URL targets a private or internal network address"))
                continue
            headers = None
            if _env("FIRECRAWL_API_KEY"):
                headers = {"Authorization": f"Bearer {_env('FIRECRAWL_API_KEY')}"}
            data = _post_json(api_url + "/v1/scrape",
                              {"url": url, "formats": ["markdown", "html"]},
                              headers=headers, timeout=60)
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            final_url = metadata.get("sourceURL", url)
            if not is_safe_url(final_url):
                results.append(failed(final_url, "Blocked: URL targets a private or internal network address"))
                continue
            md = payload.get("markdown") if isinstance(payload, dict) else None
            html = payload.get("html") if isinstance(payload, dict) else None
            chosen = md if (format == "markdown" or (format is None and md)) else (html or md or "")
            results.append({"url": final_url, "title": metadata.get("title", ""),
                            "content": chosen, "raw_content": chosen,
                            "metadata": metadata})
        except Exception as e:
            results.append(failed(url, str(e)))
    return results


def _extract(urls: list, format: str = "markdown", char_limit=None) -> dict:
    """Validate + safety-check URLs, run the best extract backend, post-process.

    Full web_extract_tool pipeline (web_tools.py): secret-param block,
    SSRF filter, provider dispatch, base64 image placeholder conversion,
    truncate-and-store with footer. Returns
    ``{"ok": True, "results": [{url, title, content, error}]}``.
    """
    normalized_urls = []
    errors = []
    for item in urls:
        if isinstance(item, dict):
            item = item.get("url") or item.get("href")
        if not isinstance(item, str) or not item.strip():
            errors.append({"url": "", "title": "", "content": "",
                           "error": "Invalid URL item: expected a URL string or an object with a string 'url' or 'href' field"})
            continue
        normalized = normalize_url_for_request(item)
        for candidate in (item, unquote(item), normalized, unquote(normalized)):
            if _SECRET_PREFIX_RE.search(candidate):
                return {"ok": False, "error": "Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs."}
        sensitive = sensitive_query_param_name(normalized)
        if sensitive:
            return {"ok": False, "error": f"Blocked: URL contains a credential-like query parameter ({sensitive})."}
        safe = is_safe_url(normalized)
        if not safe:
            errors.append({"url": normalized, "title": "", "content": "",
                           "error": "Blocked: URL targets a private or internal network address"})
            continue
        normalized_urls.append(normalized)

    backend = _get_backend()
    if not normalized_urls:
        results = []
    elif backend == "searxng" or backend == "brave-free":
        # Search-only backends cannot extract (provider supports_extract False).
        results = [{"url": u, "title": "", "content": "",
                    "error": f"{backend} is a search-only backend and cannot extract URL content. "
                             "Set web.backend to tavily or firecrawl, or set TAVILY_API_KEY / FIRECRAWL_API_KEY."}
                   for u in normalized_urls]
    elif backend in ("tavily", "firecrawl"):
        try:
            results = _provider_extract(normalized_urls, backend, format)
        except Exception as e:
            results = [{"url": u, "title": "", "content": "",
                        "error": f"Extract failed: {e}"} for u in normalized_urls]
    else:
        results = []

    # Reconstruct the original input order across invalid and blocked entries.
    if errors:
        by_index = {i: errors[i] for i in range(len(errors))}
        valid_pos = 0
        for i in range(len(urls)):
            if i in by_index:
                continue
            by_index[i] = results[valid_pos] if valid_pos < len(results) else {
                "url": normalized_urls[valid_pos] if valid_pos < len(normalized_urls) else "",
                "title": "", "content": "", "error": "Extract backend returned no result for this URL",
            }
            valid_pos += 1
        results = [by_index[i] for i in range(len(urls))]

    effective_limit = char_limit if char_limit is not None else _get_extract_char_limit()
    try:
        effective_limit = max(2000, min(int(effective_limit), 500_000))
    except (TypeError, ValueError):
        effective_limit = DEFAULT_EXTRACT_CHAR_LIMIT

    trimmed = []
    for result in results:
        entry = {"url": result.get("url", ""), "title": result.get("title", ""),
                 "content": result.get("content", ""), "error": result.get("error")}
        if result.get("error"):
            trimmed.append(entry)
            continue
        raw = result.get("raw_content", "") or result.get("content", "")
        if not raw:
            trimmed.append(entry)
            continue
        clean = convert_base64_images_to_links(raw)
        model_text, _truncated = _truncate_with_footer(clean, entry["url"], effective_limit)
        entry["content"] = model_text
        trimmed.append(entry)
    return {"ok": True, "results": trimmed}


# ── Public API ─────────────────────────────────────────────────────────────

def web_search(query: str, k: int = 5) -> dict:
    """Search the web; returns {"ok": True, "results": [...]} or an error.

    Result items carry ``title``, ``url``, ``description``, ``position``
    (web_search_tool canonical shape). Graceful when no backend is
    configured.
    """
    if not query or not query.strip():
        return {"ok": False, "error": "query is required"}
    try:
        k = min(max(int(k), 1), 100)
    except (TypeError, ValueError):
        k = 5
    if not _available_backends():
        return {"ok": False, "error": "No web search provider configured. "
                                      "Set TAVILY_API_KEY, EXA_API_KEY, PARALLEL_API_KEY, "
                                      "FIRECRAWL_API_KEY, or SEARXNG_URL, or web.backend in settings."}
    results, backend = _search_fallback_chain(query.strip(), k)
    if results is None:
        return {"ok": False, "error": "web search failed on all configured backends"}
    return {"ok": True, "results": results, "backend": backend}


def web_fetch(url) -> dict:
    """Fetch one URL; returns {"ok": True, "content": str} clean page text.

    Accepts a URL string or a search-result dict with ``url``/``href``,
    mirroring web_extract_tool's item handling (max 1 URL per call for the
    ``content`` contract).
    """
    if isinstance(url, dict):
        url = url.get("url") or url.get("href")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "Invalid URL item: expected a URL string or an object with a string 'url' or 'href' field"}
    target = normalize_url_for_request(url)
    if sensitive_query_param_name(target):
        return {"ok": False, "error": "Blocked: URL contains a credential-like query parameter"}
    if not is_safe_url(target):
        return {"ok": False, "error": "Blocked: URL targets a private or internal network address"}
    resp = _extract([target], format="markdown")
    if not resp.get("ok"):
        return resp
    results = resp.get("results", [])
    for r in results:
        if r.get("url") == target and not r.get("error"):
            return {"ok": True, "content": r.get("content", ""), "title": r.get("title", "")}
    error = next((r.get("error") for r in results if r.get("error")), "content was inaccessible or not found")
    return {"ok": False, "error": error}


def url_safety_check(url: str) -> dict:
    """Return {"ok": True, "safe": bool, "reason": str} for one URL.

    Same policy as Hermes is_safe_url, with a human-readable reason for
    blocking. Never raises for malformed input.
    """
    if not isinstance(url, str) or not url.strip():
        return {"ok": True, "safe": False, "reason": "empty or invalid URL"}
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    scheme = (parsed.scheme or "").strip().lower()
    if scheme not in {"http", "https"}:
        return {"ok": True, "safe": False, "reason": f"unsupported URL scheme: {scheme or '<empty>'}"}
    if not hostname:
        return {"ok": True, "safe": False, "reason": "URL has no hostname"}
    if hostname in _BLOCKED_HOSTNAMES:
        return {"ok": True, "safe": False, "reason": "blocked cloud-metadata hostname"}
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return {"ok": True, "safe": not (_proxy_is_configured()),
                "reason": "DNS resolution failed" + ("; delegated to configured proxy" if _proxy_is_configured() else "")}
    except Exception as e:
        return {"ok": True, "safe": False, "reason": f"safety check error: {e}"}
    allow_all_private = _global_allow_private_urls()
    for _family, _, _, _, sockaddr in addr_info[:8]:
        ip_str = sockaddr[0].split("%")[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return {"ok": True, "safe": False, "reason": f"unparseable IP address {ip_str!r}"}
        if ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS):
            return {"ok": True, "safe": False, "reason": "cloud-metadata / link-local address"}
        if not allow_all_private and _is_blocked_ip(ip):
            return {"ok": True, "safe": False, "reason": f"private or internal address ({ip_str})"}
    return {"ok": True, "safe": True, "reason": "public address"}