#!/usr/bin/env python3
"""Atropos Home Assistant tools — HA REST API, stdlib only.

Ported from hermes-agent/tools/homeassistant_tool.py. Same endpoints,
same validation, same security rules:

- GET  {HASS_URL}/api/states            -> ha_states() filter by domain/area
- GET  {HASS_URL}/api/states/{id}       -> ha_entity() with full attributes
- GET  {HASS_URL}/api/services          -> ha_list_services()
- POST {HASS_URL}/api/services/{d}/{s}  -> ha_call_service()

Credentials: ``HASS_URL`` (default http://homeassistant.local:8123) and a
Long-Lived Access Token in ``HASS_TOKEN``. The entity_id regex
(``^[a-z_][a-z0-9_]*`` dot ``[a-z0-9_]+$``), service-name regex, and the blocked
service domains (shell_command, command_line, python_script, pyscript,
hassio, rest_command) are copied verbatim from the Hermes source — they
prevent path traversal in the URL and arbitrary code execution on the HA
host.

Deliberate deviations:
- aiohttp -> urllib.request (stdlib). Timeouts preserved (15s list, 10s
  state, 15s service; 30s cap on the sync wrapper).
- No async plumbing: ``_run_async`` is dropped; the sync calls map 1:1.
- Return shape is dict-based ({ok, result} / {ok, affected_entities})
  instead of the JSON-string tool envelope; handlers expose result dicts
  directly.
"""

import json
import os
import re
import urllib.error
import urllib.request

# _HASS_URL/_HASS_TOKEN legacy module slots kept for monkeypatching compat:
_HASS_URL: str = ""
_HASS_TOKEN: str = ""

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BLOCKED_DOMAINS = frozenset({
    "shell_command",
    "command_line",
    "python_script",
    "pyscript",
    "hassio",
    "rest_command",
})


def _get_config():
    """Return (hass_url, hass_token) from env vars at call time."""
    return (
        (_HASS_URL or os.getenv("HASS_URL", "http://homeassistant.local:8123")).rstrip("/"),
        _HASS_TOKEN or os.getenv("HASS_TOKEN", ""),
    )


def _headers(token: str = ""):
    """Return Authorization headers for the HA REST API."""
    if not token:
        _, token = _get_config()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _available() -> bool:
    """Tool is only usable when HASS_TOKEN is set (homeassistant_tool)."""
    return bool(_get_config()[1])


def _request_json(method: str, path: str, payload=None, timeout: int = 15):
    """Send one HA REST request; returns parsed JSON. Raises on HTTP errors."""
    hass_url, hass_token = _get_config()
    if not hass_token:
        raise ValueError("HASS_TOKEN is not set — create a Long-Lived Access "
                         "Token in Home Assistant profile settings.")
    url = f"{hass_url}{path}"
    data = None
    headers = _headers(hass_token)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Home Assistant returned HTTP {e.code} for {method} {path}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach Home Assistant at {hass_url}: {e.reason}") from e


# ── public API ─────────────────────────────────────────────────────────────

def ha_states(domain: str = "", area: str = "") -> dict:
    """List entities, optionally filtered by domain or area.

    Returns {"ok": True, "result": {"count", "entities": [{entity_id, state,
    friendly_name}]}} — _filter_and_summarize output from homeassistant_tool.
    """
    if not _available():
        return {"ok": False, "error": "HASS_TOKEN not set — add a Long-Lived Access Token (Home Assistant profile) and set HASS_URL/HASS_TOKEN"}
    try:
        states = _request_json("GET", "/api/states")
    except Exception as e:
        return {"ok": False, "error": f"Failed to list entities: {e}"}
    if not isinstance(states, list):
        return {"ok": False, "error": "Unexpected Home Assistant response"}
    if domain:
        states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]
    if area:
        area_lower = area.lower()
        states = [
            s for s in states
            if area_lower in (s.get("attributes", {}).get("friendly_name", "") or "").lower()
            or area_lower in (s.get("attributes", {}).get("area", "") or "").lower()
        ]
    entities = [
        {
            "entity_id": s.get("entity_id", ""),
            "state": s.get("state", ""),
            "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
        }
        for s in states
    ]
    return {"ok": True, "result": {"count": len(entities), "entities": entities}}


def ha_entity(entity_id: str) -> dict:
    """Get the detailed state of one entity, attributes included.

    Returns {"ok": True, "result": {entity_id, state, attributes,
    last_changed, last_updated}}.
    """
    if not entity_id:
        return {"ok": False, "error": "Missing required parameter: entity_id"}
    if not _ENTITY_ID_RE.match(entity_id):
        return {"ok": False, "error": f"Invalid entity_id format: {entity_id}"}
    if not _available():
        return {"ok": False, "error": "HASS_TOKEN not set — add a Long-Lived Access Token (Home Assistant profile) and set HASS_URL/HASS_TOKEN"}
    try:
        data = _request_json("GET", f"/api/states/{entity_id}", timeout=10)
    except Exception as e:
        return {"ok": False, "error": f"Failed to get state for {entity_id}: {e}"}
    return {
        "ok": True,
        "result": {
            "entity_id": data.get("entity_id", entity_id),
            "state": data.get("state", ""),
            "attributes": data.get("attributes", {}),
            "last_changed": data.get("last_changed"),
            "last_updated": data.get("last_updated"),
        },
    }


def ha_list_services(domain: str = "") -> dict:
    """List available HA services (actions), optionally filtered by domain.

    Compact output per domain: {"count", "domains": [{domain, services: {
    name: {description, fields: {name: description}}}}]}.
    """
    if not _available():
        return {"ok": False, "error": "HASS_TOKEN not set — add a Long-Lived Access Token (Home Assistant profile) and set HASS_URL/HASS_TOKEN"}
    try:
        services = _request_json("GET", "/api/services")
    except Exception as e:
        return {"ok": False, "error": f"Failed to list services: {e}"}
    if not isinstance(services, list):
        return {"ok": False, "error": "Unexpected Home Assistant response"}
    if domain:
        services = [s for s in services if s.get("domain") == domain]
    result = []
    for svc_domain in services:
        d = svc_domain.get("domain", "")
        domain_services = {}
        for svc_name, svc_info in (svc_domain.get("services", {}) or {}).items():
            svc_entry = {"description": (svc_info or {}).get("description", "")}
            fields = (svc_info or {}).get("fields", {})
            if fields:
                svc_entry["fields"] = {
                    k: v.get("description", "") for k, v in fields.items()
                    if isinstance(v, dict)
                }
            domain_services[svc_name] = svc_entry
        result.append({"domain": d, "services": domain_services})
    return {"ok": True, "result": {"count": len(result), "domains": result}}


def ha_call_service(domain: str, service: str, entity_id: str = None,
                    data=None) -> dict:
    """Call a Home Assistant service (turn_on, set_temperature, ...).

    Returns {"ok": True, "affected_entities": [{entity_id, state}]}.
    ``data`` may be a dict or a JSON string. Blocked domains and invalid
    domain/service/entity_id names are rejected before any request is
    made (homeassistant_tool validation order).
    """
    domain = (domain or "").strip()
    service = (service or "").strip()
    if not domain or not service:
        return {"ok": False, "error": "Missing required parameters: domain and service"}
    if not _SERVICE_NAME_RE.match(domain):
        return {"ok": False, "error": f"Invalid domain format: {domain!r}"}
    if not _SERVICE_NAME_RE.match(service):
        return {"ok": False, "error": f"Invalid service format: {service!r}"}
    if domain in _BLOCKED_DOMAINS:
        return {"ok": False, "error": f"Service domain '{domain}' is blocked for security. "
                                      f"Blocked domains: {', '.join(sorted(_BLOCKED_DOMAINS))}"}
    if entity_id and not _ENTITY_ID_RE.match(entity_id):
        return {"ok": False, "error": f"Invalid entity_id format: {entity_id}"}
    if isinstance(data, str):
        try:
            data = json.loads(data) if data.strip() else None
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"Invalid JSON string in 'data' parameter: {e}"}
    elif not isinstance(data, dict):
        data = None
    if not _available():
        return {"ok": False, "error": "HASS_TOKEN not set — add a Long-Lived Access Token (Home Assistant profile) and set HASS_URL/HASS_TOKEN"}
    payload = dict(data) if data else {}
    if entity_id:
        payload["entity_id"] = entity_id
    try:
        result = _request_json("POST", f"/api/services/{domain}/{service}", payload=payload)
    except Exception as e:
        return {"ok": False, "error": f"Failed to call {domain}.{service}: {e}"}
    affected = []
    if isinstance(result, list):
        for s in result:
            affected.append({"entity_id": s.get("entity_id", ""),
                             "state": s.get("state", "")})
    return {"ok": True, "service": f"{domain}.{service}",
            "affected_entities": affected}


def ha_available() -> bool:
    """Return True when HASS_TOKEN is set (check_fn equivalent)."""
    return _available()