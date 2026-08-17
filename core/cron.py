#!/usr/bin/env python3
"""Cron job management — schedule parsing, job store, script jobs.

Ported from hermes-agent/tools/cronjob_tools.py (cron/jobs.py +
cron/scheduler.py + cron/lifecycle_guard.py). Atropos is stdlib-only,
so the third-party ``croniter`` package used by Hermes for 5-field cron
expressions is replaced by a pure-Python matcher (``_cron_next``) that
implements the same Vixie-cron semantics: minute/hour/day/month/weekday
fields with ``*``, ranges, steps, and lists; ``*/5`` etc. behave exactly
like croniter for ordinary expressions (no ``@daily`` names, no second
field — the parser rejects 6-field input exactly as Hermes does when
croniter is absent).

Job storage mirrors Hermes: ``<HERMES_HOME>/cron/jobs.json`` holding
``{"jobs": [...], "updated_at": ...}`` (the real Hermes v0.9+ store).
The legacy ``*.yaml`` job files that the dashboard's ``api_cron`` still
reads are untouched — ``list_jobs`` also surfaces them so every surface
sees the same schedule set. Output for each run lands in
``<HERMES_HOME>/cron/output/<job_id>/<timestamp>.md`` with the Hermes
retention cap (50 files, ``cron.output_retention`` from config.yaml).

``context_from`` chaining is ported from scheduler.py ``_build_job_prompt``:
the most recent ``*.md`` output of each referenced job (hex id, truncated
to 8K) is injected into the job prompt. ``run_job`` executes the Hermes
no_agent path — a script under ``<HERMES_HOME>/scripts/`` (bash for
.sh/.bash, else python) whose stdout is the job result; empty stdout is
silent, non-zero exit is an error. The LLM path is out of scope here
(Atropos has no live gateway agent); agent jobs are recorded as error
with a clear message so they surface in lists instead of vanishing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config, detect

# ── constants (hermes cron/jobs.py + cron/scheduler.py) ─────────────────────
ONESHOT_GRACE_SECONDS = 120
_TICKER_INTERVAL_SECONDS = 60
_CRON_OUTPUT_DEFAULT_KEEP = 50
_DEFAULT_SCRIPT_TIMEOUT = 3600
_MAX_CONTEXT_CHARS = 8000
_SILENT_MARKER = "[SILENT]"

# Gateway-lifecycle command shapes (cron/lifecycle_guard.py). Blocking
# restart/stop/kill of the gateway prevents agent-driven SIGTERM-respawn
# loops under launchd/systemd supervision (#30719).
_GATEWAY_LIFECYCLE_RE = re.compile(
    r"(?i)"
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart)\b[^\n]*\bhermes[.\-]?gateway)"
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    r"|(?:p?kill\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:p?kill\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)

# Cron prompt threat scanner (cronjob_tools.py): strict patterns applied to
# the USER-SUPPLIED prompt at create/update time.
_CRON_THREAT_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|id_ed25519|id_ecdsa)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
    (r'/etc/sudoers|visudo', "sudoers_mod"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
]
_CRON_SECRET_VAR_RE = r'\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?'
_CRON_EXFIL_COMMAND_PATTERNS = [
    (rf'curl\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_curl_url"),
    (rf'wget\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_wget_url"),
    (rf'curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_curl_data"),
    (rf'wget\s+[^\n]*--post-(?:data|file)=[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_wget_post"),
    (rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*(?:Bearer|token)\s+{_CRON_SECRET_VAR_RE}["\']', "exfil_curl_auth_header"),
]
# Invisible-unicode markers (tools/threat_patterns.py INVISIBLE_CHARS).
_INVISIBLE_CHARS = frozenset(
    "​‌‍⁠⁢⁣⁤﻿"
    "‪‫‬‭‮⁦⁧⁨⁩"
)
_EMOJI_NEIGHBOUR_CP_RANGES = (
    (0x1F000, 0x1FFFF), (0x2600, 0x27BF), (0x2300, 0x23FF),
    (0x1F1E6, 0x1F1FF), (0x20E3, 0x20E3),
)
_VARIATION_SELECTOR_CP = 0xFE0F


# ── paths / home ────────────────────────────────────────────────────────────
def _cron_dir() -> Path:
    return detect.hermes_home() / "cron"


def _jobs_file() -> Path:
    return _cron_dir() / "jobs.json"


def _output_dir() -> Path:
    return _cron_dir() / "output"


def _job_output_dir(job_id: str) -> Path:
    """Resolve a job's output directory, rejecting any path-escape attempt."""
    text = str(job_id or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    if Path(text).is_absolute() or Path(text).drive:
        raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
    return _output_dir() / text


def _now() -> datetime:
    return datetime.now().astimezone()


def _ensure_aware(dt: datetime) -> datetime:
    """Make a datetime timezone-aware (naive = system-local wall time)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _iso(now: datetime | None = None) -> str:
    return (now or _now()).isoformat()


def _resolve_script_timeout() -> int:
    """cron/scheduler.py _get_script_timeout: env, then config, default 1h."""
    env_value = os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "").strip()
    if env_value:
        try:
            timeout = int(float(env_value))
            if timeout > 0:
                return timeout
        except Exception:
            pass
    try:
        cron_cfg = config.load().get("cron", {}) or {}
        configured = cron_cfg.get("script_timeout_seconds")
        if configured is not None:
            timeout = int(float(configured))
            if timeout > 0:
                return timeout
    except Exception:
        pass
    return _DEFAULT_SCRIPT_TIMEOUT


def _cron_output_keep() -> int:
    """Per-job output retention cap from config (``cron.output_retention``)."""
    try:
        cron_cfg = config.load().get("cron", {}) or {}
        return int(cron_cfg.get("output_retention", _CRON_OUTPUT_DEFAULT_KEEP))
    except Exception:
        return _CRON_OUTPUT_DEFAULT_KEEP


# ── atomic store writes (cron/jobs.py _save_jobs_unlocked pattern) ──────────
def _atomic_replace(tmp_path: Path, dest: Path):
    if os.name == "nt":
        if dest.exists():
            os.replace(tmp_path, dest)
        else:
            os.rename(tmp_path, dest)
    else:
        os.replace(tmp_path, dest)


def _save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp",
                                    prefix=".jobs_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(Path(tmp_path), path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp",
                                    prefix=".hb_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(Path(tmp_path), path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── schedule parsing (cron/jobs.py parse_duration / parse_schedule) ─────────
def parse_duration(s: str) -> int:
    """Parse a duration string into minutes. '30m' -> 30, '2h' -> 120, '1d' -> 1440."""
    s = s.strip().lower()
    match = re.match(r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$', s)
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Use format like '30m', '2h', or '1d'")
    value = int(match.group(1))
    unit = match.group(2)[0]  # First char: m, h, or d
    multipliers = {'m': 1, 'h': 60, 'd': 1440}
    return value * multipliers[unit]


def _cron_next(expr: str, base: datetime) -> datetime:
    """Next match of a 5-field cron expr strictly after `base`.

    Pure-stdlib replacement for croniter (Vixie-cron semantics: fields are
    minute hour day-of-month month day-of-week; ``*``, ranges ``a-b``,
    steps ``*/n`` / ``a-b/n``, and lists ``a,b`` are supported; DOW 0 and 7
    are both Sunday). Unlike croniter, a DOW range ``a-b`` spanning
    Saturday (5)-Sunday (7) is not wrapped — an exotic corner that ordinary
    cron configs never use.
    """
    def _field(pattern: str, lo: int, hi: int) -> set:
        values = set()
        for part in pattern.split(","):
            if not part:
                continue
            step = 1
            if "/" in part:
                part, _, step_s = part.partition("/")
                step = int(step_s)
                if step <= 0:
                    raise ValueError(f"Invalid step in cron field: {part}/{step_s}")
            if part == "*":
                start, end = lo, hi
            elif "-" in part:
                start_s, _, end_s = part.partition("-")
                start, end = int(start_s), int(end_s)
            else:
                start = end = int(part)
            if start < lo or end > hi or start > end:
                raise ValueError(f"Out-of-range cron field value: {part}")
            values.update(range(start, end + 1, step))
        if not values:
            raise ValueError(f"Empty cron field: {pattern}")
        return values

    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression '{expr}': expected 5 fields")
    minutes, hours, doms, months, dows = (
        _field(parts[0], 0, 59), _field(parts[1], 0, 23),
        _field(parts[2], 1, 31), _field(parts[3], 1, 12),
        _field(parts[4], 0, 7),
    )
    if 7 in dows:
        dows.update([0])
    is_star_dom = parts[2] == "*"
    is_star_dow = parts[4] == "*"

    cand = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60 + 60):
        if cand.month in months:
            # Vixie-cron day matching (croniter semantics): when exactly one
            # of DOM/DOW is restricted it decides alone; when both are
            # restricted the date matches if EITHER field matches.
            # Python weekday() is 0=Monday; cron DOW is 0=Sunday.
            cron_dow = (cand.weekday() + 1) % 7
            if is_star_dow:
                date_ok = is_star_dom or (cand.day in doms)
            elif is_star_dom:
                date_ok = cron_dow in dows
            else:
                date_ok = (cand.day in doms) or (cron_dow in dows)
            if date_ok and cand.hour in hours and cand.minute in minutes:
                return cand
        cand += timedelta(minutes=1)
    raise ValueError(f"Invalid cron expression '{expr}': no future match found")


def parse_schedule(schedule: str) -> dict:
    """Parse a schedule string into a structured dict.

    Returns dict with:
        - kind: "once" | "interval" | "cron"
        - For "once": "run_at" (ISO timestamp)
        - For "interval": "minutes" (int)
        - For "cron": "expr" (cron expression)
    Examples: "30m" -> once in 30m, "every 30m" -> interval,
    "0 9 * * *" -> cron, "2026-02-03T14:00" -> once at timestamp.
    Raises ValueError on unrecognized input (Hermes error text).
    """
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()

    # "every X" pattern -> recurring interval
    if schedule_lower.startswith("every "):
        duration_str = schedule[6:].strip()
        minutes = parse_duration(duration_str)
        return {"kind": "interval", "minutes": minutes,
                "display": f"every {minutes}m"}

    # Cron expression (5 space-separated fields). 6-field input is rejected
    # the same way Hermes behaves when croniter is missing.
    parts = schedule.split()
    if len(parts) >= 5 and all(re.match(r'^[\d\*\-,/]+$', p) for p in parts[:5]):
        if len(parts) > 5:
            raise ValueError(
                "Cron expressions require 5 fields (6-field expressions with "
                "a year are not supported in this build)")
        try:
            _cron_next(parts[0] + " " + parts[1] + " " + parts[2] + " " +
                       parts[3] + " " + parts[4], _now())
        except ValueError as e:
            raise ValueError(f"Invalid cron expression '{schedule}': {e}")
        return {"kind": "cron", "expr": schedule, "display": schedule}

    # ISO timestamp (contains T or looks like a date)
    if 'T' in schedule or re.match(r'^\d{4}-\d{2}-\d{2}', schedule):
        try:
            dt = datetime.fromisoformat(schedule.replace('Z', '+00:00'))
            # Naive timestamps become aware at parse time using the system
            # timezone so the stored instant matches the user's wall clock
            # (hermes cron/jobs.py #51021).
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_now().tzinfo)
            return {"kind": "once", "run_at": dt.isoformat(),
                    "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}"}
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")

    # Duration like "30m", "2h", "1d" -> one-shot from now
    try:
        minutes = parse_duration(schedule)
        run_at = _now() + timedelta(minutes=minutes)
        return {"kind": "once", "run_at": run_at.isoformat(),
                "display": f"once in {original}"}
    except ValueError:
        pass

    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - Duration: '30m', '2h', '1d' (one-shot)\n"
        f"  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        f"  - Cron: '0 9 * * *' (cron expression)\n"
        f"  - Timestamp: '2026-02-03T14:00:00' (one-shot at time)")


def _grace_seconds(schedule: dict) -> int:
    """How late a job can be and still catch up (cron/jobs.py).

    Half the schedule period, clamped between 120s and 2h.
    """
    MIN_GRACE = 120
    MAX_GRACE = 7200
    kind = schedule.get("kind")
    if kind == "interval":
        period = schedule.get("minutes", 1) * 60
        return max(MIN_GRACE, min(period // 2, MAX_GRACE))
    if kind == "cron" and schedule.get("expr"):
        try:
            base = _now()
            first = _cron_next(schedule["expr"], base)
            second = _cron_next(schedule["expr"], first)
            period = int((second - first).total_seconds())
            return max(MIN_GRACE, min(period // 2, MAX_GRACE))
        except Exception:
            pass
    return MIN_GRACE


def _recoverable_oneshot_run_at(schedule: dict, now: datetime,
                                last_run_at: str | None = None) -> str | None:
    """One-shot run time if still eligible: not yet run and inside the
    120s grace window past run_at (cron/jobs.py)."""
    if not isinstance(schedule, dict) or schedule.get("kind") != "once":
        return None
    if last_run_at:
        return None
    run_at = schedule.get("run_at")
    if not run_at:
        return None
    try:
        run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
    except Exception:
        return None
    if run_at_dt >= now - timedelta(seconds=ONESHOT_GRACE_SECONDS):
        return run_at
    return None


def next_run(spec: dict | str, after_ts: float | str | None = None) -> float | None:
    """Compute the next run for a schedule.

    ``spec`` may be a parsed schedule dict (parse_schedule output) or a raw
    schedule string (parsed on the fly). ``after_ts`` is an epoch float or an
    ISO timestamp used as the anchor (the job's last run); when omitted the
    current time is used. Returns an epoch float, or None when the schedule
    has no future run (a one-shot past its grace window).
    """
    if isinstance(spec, str):
        try:
            spec = parse_schedule(spec)
        except ValueError:
            return None
    if not isinstance(spec, dict):
        return None
    kind = spec.get("kind")
    if kind is None:
        return None

    if after_ts is None:
        now = _now()
        last_run_at = None
    elif isinstance(after_ts, (int, float)):
        now = datetime.fromtimestamp(after_ts, tz=_now().tzinfo)
        last_run_at = _iso(now)
    else:
        try:
            last = _ensure_aware(datetime.fromisoformat(str(after_ts)))
        except ValueError:
            return None
        now = _now()
        last_run_at = _iso(last)

    if kind == "once":
        run_at = _recoverable_oneshot_run_at(spec, now, last_run_at=last_run_at)
        if run_at is None:
            return None
        return _ensure_aware(datetime.fromisoformat(run_at)).timestamp()

    if kind == "interval":
        minutes = spec.get("minutes")
        if minutes is None:
            return None
        if last_run_at:
            try:
                last = _ensure_aware(datetime.fromisoformat(last_run_at))
                nxt = last + timedelta(minutes=minutes)
            except Exception:
                nxt = now + timedelta(minutes=minutes)
        else:
            nxt = now + timedelta(minutes=minutes)  # first run: now + interval
        return nxt.timestamp()

    if kind == "cron":
        expr = spec.get("expr")
        if not expr:
            return None
        try:
            base = _now()
            if last_run_at:
                try:
                    base = _ensure_aware(datetime.fromisoformat(last_run_at))
                except Exception:
                    base = base
            return _cron_next(expr, base).timestamp()
        except ValueError:
            return None
    return None


def next_run_iso(spec: dict | str, after_ts: str | None = None) -> str | None:
    """next_run() as an ISO string (the shape create_job stores)."""
    ts = next_run(spec, after_ts)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=_now().tzinfo).isoformat()


# ── store load/save (cron/jobs.py load_jobs / save_jobs) ────────────────────
def _load_json() -> dict:
    path = _jobs_file()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def load_jobs() -> list:
    """Load all jobs from storage ({} or bare-list stores auto-accepted)."""
    data = _load_json()
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        return jobs if isinstance(jobs, list) else []
    if isinstance(data, list):
        return data
    return []


def save_jobs(jobs: list) -> None:
    """Save all jobs to storage (atomic tmpfile + replace)."""
    _save_json(_jobs_file(), {"jobs": jobs, "updated_at": _iso()})


def _next_job_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def _normalize_skill_list(skill=None, skills=None) -> list:
    """Normalize legacy single-skill and multi-skill inputs into an
    ordered, deduped list (cron/jobs.py)."""
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)
    normalized = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _canonical_skills(skill=None, skills=None) -> list:
    """Alias of _normalize_skill_list kept for tools.py parity."""
    return _normalize_skill_list(skill, skills)


def _coerce_job_text(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def _schedule_display_for_job(job: dict) -> str:
    display = _coerce_job_text(job.get("schedule_display")).strip()
    if display:
        return display
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        for key in ("display", "value", "expr", "run_at"):
            text = _coerce_job_text(schedule.get(key)).strip()
            if text:
                return text
    elif schedule is not None:
        return str(schedule)
    return "?"


def _normalize_job_record(job: dict) -> dict:
    """Read-safe job shape: never crash on nullable legacy fields
    (cron/jobs.py _normalize_job_record)."""
    skills = _normalize_skill_list(job.get("skill"), job.get("skills"))
    normalized = dict(job)
    normalized["skills"] = skills
    normalized["skill"] = skills[0] if skills else None
    job_id = _coerce_job_text(normalized.get("id"), "unknown")
    prompt = _coerce_job_text(normalized.get("prompt"))
    normalized["id"] = job_id
    normalized["prompt"] = prompt
    name = _coerce_job_text(normalized.get("name")).strip()
    if not name:
        script = _coerce_job_text(normalized.get("script")).strip()
        label_source = (prompt or (skills[0] if skills else "") or script
                        or job_id or "cron job")
        name = label_source[:50].strip() or "cron job"
    normalized["name"] = name
    normalized["schedule_display"] = _schedule_display_for_job(normalized)
    state = _coerce_job_text(normalized.get("state")).strip()
    if not state:
        state = "scheduled" if normalized.get("enabled", True) else "paused"
    normalized["state"] = state
    return normalized


# ── threat scanning (cronjob_tools.py _scan_cron_prompt etc.) ───────────────
def _is_emoji_cp(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_NEIGHBOUR_CP_RANGES)


def _zwj_has_emoji_neighbour(text: str, idx: int) -> bool:
    """True when the ZWJ at idx sits inside an emoji grapheme cluster."""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    return (left >= 0 and right < len(text)
            and _is_emoji_cp(ord(text[left])) and _is_emoji_cp(ord(text[right])))


def _strip_cron_safe_constructs(prompt: str) -> str:
    """Scrub the GitHub `Authorization: token $GITHUB_TOKEN` auth-header
    pattern so it does not trip the broader curl-auth-header exfil rule."""
    return re.sub(
        rf'curl\s+[^\n;&|$`]*(?:-H|--header)\s+["\']Authorization:\s*token\s+{_CRON_SECRET_VAR_RE}["\']'
        r'\s+["\']?https://api\.github\.com(?::\d+)?(?:/|\s|$|["\'])[^\s;&|$`]*',
        'curl https://api.github.com/user',
        prompt,
        flags=re.IGNORECASE,
    )


def _strip_legitimate_emoji_zwj(prompt: str) -> str:
    if '‍' not in prompt:
        return prompt
    cleaned = []
    for idx, ch in enumerate(prompt):
        if ch == '‍' and _zwj_has_emoji_neighbour(prompt, idx):
            continue
        cleaned.append(ch)
    return ''.join(cleaned)


def _scan_cron_prompt(prompt: str) -> str:
    """Scan a USER-SUPPLIED cron prompt for injection/exfiltration.

    Returns an error string when blocked, else empty string. Uses the
    exact Hermes pattern set; invisible unicode blocks the job.
    """
    if not prompt:
        return ""
    prompt_to_scan = _strip_cron_safe_constructs(prompt)
    prompt_for_invisible = _strip_legitimate_emoji_zwj(prompt_to_scan)
    for char in _INVISIBLE_CHARS:
        if char in prompt_for_invisible:
            return (f"Blocked: prompt contains invisible unicode "
                    f"U+{ord(char):04X} (possible injection).")
    for pattern, pid in _CRON_THREAT_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return (f"Blocked: prompt matches threat pattern '{pid}'. "
                    f"Cron prompts must not contain injection or exfiltration payloads.")
    for pattern, pid in _CRON_EXFIL_COMMAND_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return (f"Blocked: prompt matches threat pattern '{pid}'. "
                    f"Cron prompts must not contain injection or exfiltration payloads.")
    return ""


def _strip_invisible_unicode(prompt: str) -> tuple:
    """Strip invisible-unicode chars, preserving emoji ZWJ joiners.
    Returns (cleaned, sorted list of 'U+XXXX' labels removed)."""
    if not prompt:
        return prompt, []
    removed = set()
    cleaned = []
    for idx, ch in enumerate(prompt):
        if ch in _INVISIBLE_CHARS:
            if ch == '‍' and _zwj_has_emoji_neighbour(prompt, idx):
                cleaned.append(ch)
                continue
            removed.add(f"U+{ord(ch):04X}")
            continue
        cleaned.append(ch)
    return ''.join(cleaned), sorted(removed)


def _scan_cron_skill_assembled(assembled: str) -> tuple:
    """Scan an ASSEMBLED prompt (skill bodies injected): loose pattern set,
    invisible unicode sanitized rather than blocked. Returns (cleaned, error)."""
    cleaned, _removed = _strip_invisible_unicode(assembled)
    prompt_to_scan = _strip_cron_safe_constructs(cleaned)
    for pattern, pid in _CRON_SKILL_ASSEMBLED_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return cleaned, (f"Blocked: prompt matches threat pattern '{pid}'. "
                             f"Cron prompts must not contain injection or exfiltration payloads.")
    return cleaned, ""


# Looser pattern set for skill-assembled prompts (cronjob_tools.py): command
# shapes are dropped because security docs / postmortems quote them in prose.
_CRON_SKILL_ASSEMBLED_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
]


def _check_gateway_lifecycle(prompt: str | None, script: str | None = None) -> None:
    """Raise ValueError if prompt or script contains a gateway-lifecycle
    command (cron/lifecycle_guard.py #30719)."""
    combined = prompt or ""
    if script:
        raw = Path(script).expanduser()
        path = raw if raw.is_absolute() else detect.hermes_home() / "scripts" / raw
        try:
            script_text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            script_text = ""
        if script_text:
            combined = f"{combined}\n{script_text}"
    if _GATEWAY_LIFECYCLE_RE.search(combined):
        raise ValueError(
            "Blocked: cron job contains a gateway lifecycle command "
            "(restart/stop/kill). This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision. "
            "Run the gateway restart from a shell outside the running "
            "gateway instead.")


def _validate_script_path(script: str | None) -> str | None:
    """Validate a cron job script path at the API boundary: only relative
    paths inside <HERMES_HOME>/scripts/ are allowed (cronjob_tools.py)."""
    if not script or not script.strip():
        return None
    raw = script.strip()
    if raw.startswith(("/", "~")) or (len(raw) >= 2 and raw[1] == ":"):
        return (f"Script path must be relative to the scripts directory. "
                f"Got absolute or home-relative path: {raw!r}. "
                f"Place scripts in the scripts dir and use just the filename.")
    scripts_dir = detect.hermes_home() / "scripts"
    try:
        resolved = (scripts_dir / raw).resolve()
        resolved.relative_to(scripts_dir.resolve())
    except (ValueError, OSError):
        return f"Script path escapes the scripts directory via traversal: {raw!r}"
    return None


def _normalize_workdir(workdir: str | None) -> str | None:
    """Normalize and validate a workdir: absolute, existing, a directory
    (cron/jobs.py). Raises ValueError on invalid input."""
    if workdir is None:
        return None
    raw = str(workdir).strip()
    if not raw:
        return None
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            f"Cron workdir must be an absolute path (got {raw!r}). "
            f"Cron jobs run detached from any shell cwd, so relative paths are ambiguous.")
    resolved = expanded.resolve()
    if not resolved.exists():
        raise ValueError(f"Cron workdir does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Cron workdir is not a directory: {resolved}")
    return str(resolved)


def _normalize_deliver_param(value) -> str | None:
    """Flatten list/tuple deliver values into the canonical comma string
    (cronjob_tools.py)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value if str(p).strip()]
        return ",".join(parts) if parts else None
    text = str(value).strip()
    return text or None


# ── job CRUD (cron/jobs.py create_job / get_job / update_job / ...) ─────────
def create_job(prompt: str | None, schedule: str, name: str | None = None,
               repeat: int | None = None, deliver: str | None = None,
               skill: str | None = None, skills=None,
               model: str | None = None, provider: str | None = None,
               base_url: str | None = None, script: str | None = None,
               context_from=None, no_agent: bool = False,
               workdir: str | None = None,
               enabled_toolsets=None, origin=None) -> dict:
    """Create a new cron job (cron/jobs.py create_job).

    Raises ValueError on invalid schedules, one-shot times outside the
    grace window, or blocked content (threat scan / gateway lifecycle).
    """
    if not schedule:
        raise ValueError("schedule is required for create")
    parsed = parse_schedule(schedule)

    if repeat is not None and repeat <= 0:
        repeat = None
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1
    if deliver is None:
        deliver = "origin" if origin else "local"

    job_id = _next_job_id()
    now = _iso()

    normalized_skills = _normalize_skill_list(skill, skills)
    normalized_model = (str(model).strip() or None) if model is not None else None
    normalized_provider = (str(provider).strip() or None) if provider is not None else None
    normalized_base_url = ((str(base_url).strip().rstrip("/") or None)
                           if base_url is not None else None)
    normalized_script = (str(script).strip() or None) if isinstance(script, str) else None
    normalized_toolsets = None
    if enabled_toolsets:
        normalized_toolsets = [str(t).strip() for t in enabled_toolsets if str(t).strip()]
        normalized_toolsets = normalized_toolsets or None
    normalized_workdir = _normalize_workdir(workdir)
    normalized_no_agent = bool(no_agent)

    if normalized_no_agent and not normalized_script:
        raise ValueError(
            "no_agent=True requires a script — with no agent and no script "
            "there is nothing for the job to run.")

    if isinstance(context_from, str):
        context_from = [context_from.strip()] if context_from.strip() else None
    elif isinstance(context_from, list):
        context_from = [str(j).strip() for j in context_from if str(j).strip()] or None
    else:
        context_from = None

    prompt_text = _coerce_job_text(prompt)

    # Prompt content gates (cronjob_tools.py create + lifecycle_guard.py).
    if prompt_text:
        scan_error = _scan_cron_prompt(prompt_text)
        if scan_error:
            raise ValueError(scan_error)
    _check_gateway_lifecycle(prompt_text, normalized_script)
    script_error = _validate_script_path(normalized_script)
    if script_error:
        raise ValueError(script_error)

    # context_from must reference existing jobs (cronjob_tools.py).
    if context_from:
        existing = {j.get("id") for j in load_jobs()}
        for ref_id in context_from:
            if ref_id not in existing:
                raise ValueError(
                    f"context_from job '{ref_id}' not found. "
                    f"List jobs to see available IDs.")

    label_source = (prompt_text or (normalized_skills[0] if normalized_skills else None)
                    or (normalized_script if normalized_no_agent else None)) or "cron job"

    next_run_at = next_run_iso(parsed)
    if parsed.get("kind") == "once" and next_run_at is None:
        run_at = parsed.get("run_at") or schedule
        raise ValueError(
            f"Requested one-shot time {run_at} is more than "
            f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled.")

    job = {
        "id": job_id,
        "name": name or label_source[:50].strip(),
        "prompt": prompt_text,
        "skills": normalized_skills,
        "skill": normalized_skills[0] if normalized_skills else None,
        "model": normalized_model,
        "provider": normalized_provider,
        "base_url": normalized_base_url,
        "script": normalized_script,
        "no_agent": normalized_no_agent,
        "context_from": context_from,
        "schedule": parsed,
        "schedule_display": parsed.get("display", schedule),
        "repeat": {"times": repeat, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": now,
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": deliver,
        "origin": origin,
        "enabled_toolsets": normalized_toolsets,
        "workdir": normalized_workdir,
    }
    jobs = load_jobs()
    jobs.append(job)
    save_jobs(jobs)
    return job


def get_job(job_id: str) -> dict | None:
    """Get a job by exact ID."""
    for job in load_jobs():
        if job.get("id") == job_id:
            return _normalize_job_record(job)
    return None


class AmbiguousJobReference(LookupError):
    """Raised when a job name matches more than one job."""

    def __init__(self, ref: str, matches: list):
        self.ref = ref
        self.matches = matches
        ids = ", ".join(m["id"] for m in matches)
        super().__init__(
            f"Job name '{ref}' is ambiguous — matches {len(matches)} jobs: {ids}. "
            f"Use the job ID instead.")


def resolve_job_ref(ref: str) -> dict | None:
    """Resolve a job reference: exact ID wins, then case-insensitive name.
    Raises AmbiguousJobReference when a name matches multiple jobs."""
    if not ref:
        return None
    jobs = load_jobs()
    for job in jobs:
        if job.get("id") == ref:
            return _normalize_job_record(job)
    ref_lower = ref.lower()
    name_matches = [j for j in jobs if (j.get("name") or "").lower() == ref_lower]
    if not name_matches:
        return None
    if len(name_matches) > 1:
        raise AmbiguousJobReference(ref, [_normalize_job_record(j) for j in name_matches])
    return _normalize_job_record(name_matches[0])


def list_jobs(include_disabled: bool = False) -> list:
    """List all jobs, optionally including disabled ones (cron/jobs.py)."""
    jobs = [_normalize_job_record(j) for j in load_jobs()]
    if not include_disabled:
        jobs = [j for j in jobs if j.get("enabled", True)]
    return jobs


def update_job(job_id: str, updates: dict) -> dict | None:
    """Update a job by ID, refreshing derived schedule fields when needed
    (cron/jobs.py update_job)."""
    # Block mutation of immutable fields (id is a filesystem path component).
    bad_fields = {"id", "created_at"}.intersection(updates or {})
    if bad_fields:
        raise ValueError(
            f"Cron job field(s) cannot be updated: {', '.join(sorted(bad_fields))}")

    jobs = load_jobs()
    for i, job in enumerate(jobs):
        if job.get("id") != job_id:
            continue

        if "workdir" in updates:
            wd = updates["workdir"]
            if wd in {None, "", False}:
                updates["workdir"] = None
            else:
                updates["workdir"] = _normalize_workdir(wd)

        if "script" in updates:
            script_error = _validate_script_path(updates.get("script"))
            if script_error:
                raise ValueError(script_error)

        if "prompt" in updates and updates["prompt"] is not None:
            scan_error = _scan_cron_prompt(str(updates["prompt"]))
            if scan_error:
                raise ValueError(scan_error)

        updated = dict(job)
        updated.update(updates)
        updated = _apply_skill_fields(updated)
        schedule_changed = "schedule" in updates
        updated["id"] = job_id

        if "skills" in updates or "skill" in updates:
            normalized_skills = _normalize_skill_list(updated.get("skill"),
                                                      updated.get("skills"))
            updated["skills"] = normalized_skills
            updated["skill"] = normalized_skills[0] if normalized_skills else None

        if schedule_changed:
            updated_schedule = updated["schedule"]
            if isinstance(updated_schedule, str):
                updated_schedule = parse_schedule(updated_schedule)
                updated["schedule"] = updated_schedule
            updated["schedule_display"] = updates.get(
                "schedule_display",
                updated_schedule.get("display", updated.get("schedule_display")))
            if updated.get("state") != "paused":
                updated_next_run = next_run_iso(updated_schedule)
                if (updated_next_run is None
                        and updated_schedule.get("kind") == "once"):
                    run_at = updated_schedule.get("run_at") or updated_schedule
                    raise ValueError(
                        f"Requested one-shot time {run_at} is more than "
                        f"{ONESHOT_GRACE_SECONDS}s in the past and cannot be scheduled.")
                updated["next_run_at"] = updated_next_run

        if (updated.get("enabled", True) and updated.get("state") != "paused"
                and not updated.get("next_run_at")):
            next_run_ts = next_run_iso(updated["schedule"])
            if next_run_ts is None and updated["schedule"].get("kind") == "once":
                run_at = updated["schedule"].get("run_at", "unknown")
                raise ValueError(
                    f"Requested one-shot time {run_at} is in the past "
                    f"(grace window: {ONESHOT_GRACE_SECONDS}s) and cannot be scheduled.")
            updated["next_run_at"] = next_run_ts

        jobs[i] = updated
        save_jobs(jobs)
        return _normalize_job_record(jobs[i])
    return None


def _apply_skill_fields(job: dict) -> dict:
    normalized = dict(job)
    skills = _normalize_skill_list(normalized.get("skill"), normalized.get("skills"))
    normalized["skills"] = skills
    normalized["skill"] = skills[0] if skills else None
    return normalized


def pause_job(job_id: str, reason: str | None = None) -> dict | None:
    """Pause a job without deleting it. Accepts a job ID or name."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    return update_job(job["id"], {
        "enabled": False,
        "state": "paused",
        "paused_at": _iso(),
        "paused_reason": reason,
    })


def resume_job(job_id: str) -> dict | None:
    """Resume a paused job and compute the next future run from now."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    next_run_at = next_run_iso(job["schedule"])
    if next_run_at is None and job["schedule"].get("kind") == "once":
        run_at = job["schedule"].get("run_at", "unknown")
        raise ValueError(
            f"Cannot resume: one-shot time {run_at} is in the past "
            f"(grace window: {ONESHOT_GRACE_SECONDS}s) and will never fire.")
    return update_job(job["id"], {
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "next_run_at": next_run_at,
    })


def trigger_job(job_id: str) -> dict | None:
    """Schedule a job to run on the next scheduler tick."""
    job = resolve_job_ref(job_id)
    if not job:
        return None
    return update_job(job["id"], {
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "next_run_at": _iso(),
    })


def remove_job(job_id: str) -> bool:
    """Remove a job by ID or name; cleans up its output directory."""
    job = resolve_job_ref(job_id)
    if not job:
        return False
    canonical_id = job["id"]
    jobs = load_jobs()
    original_len = len(jobs)
    jobs = [j for j in jobs if j.get("id") != canonical_id]
    if len(jobs) < original_len:
        job_output_dir = _job_output_dir(canonical_id)
        save_jobs(jobs)
        if job_output_dir.exists():
            shutil.rmtree(job_output_dir, ignore_errors=True)
        return True
    return False


def mark_job_run(job_id: str, success: bool, error: str | None = None,
                 delivery_error: str | None = None) -> None:
    """Mark a job as having been run (cron/jobs.py mark_job_run).

    Updates last_run_at/last_status, increments the completed count,
    computes next_run_at, auto-removes finite one-shots at their repeat
    limit, and marks recurring jobs state=error when the next run cannot
    be computed (never silently disables a recurring job).
    """
    jobs = load_jobs()
    for i, job in enumerate(jobs):
        if job.get("id") != job_id:
            continue
        now = _iso()
        job["last_run_at"] = now
        job["last_status"] = "ok" if success else "error"
        job["last_error"] = error if not success else None
        job["last_delivery_error"] = delivery_error
        job["fire_claim"] = None
        if job.get("run_claim") is not None:
            job["run_claim"] = None

        if job.get("repeat"):
            repeat = job["repeat"]
            times = repeat.get("times")
            completed = repeat.get("completed", 0)
            kind = job.get("schedule", {}).get("kind")
            preclaimed_oneshot = (kind == "once" and times is not None
                                  and times > 0 and completed > 0)
            if not preclaimed_oneshot:
                completed += 1
                repeat["completed"] = completed
            if times is not None and times > 0 and completed >= times:
                jobs.pop(i)
                save_jobs(jobs)
                return

        job["next_run_at"] = next_run_iso(job.get("schedule"), now)

        if job["next_run_at"] is None:
            kind = job.get("schedule", {}).get("kind")
            if kind in {"cron", "interval"}:
                job["state"] = "error"
                if not job.get("last_error"):
                    job["last_error"] = (
                        "Failed to compute next run for recurring "
                        "schedule.")
            else:
                job["enabled"] = False
                job["state"] = "completed"
        elif job.get("state") != "paused":
            job["state"] = "scheduled"

        save_jobs(jobs)
        return


# ── output store (cron/jobs.py save_job_output / _prune_job_output) ─────────
def _prune_job_output(job_output_dir: Path, keep: int) -> int:
    """Remove the oldest timestamp-named run-output files beyond `keep`."""
    if keep <= 0:
        return 0
    try:
        files = sorted((f for f in job_output_dir.glob("*.md") if f.is_file()),
                       key=lambda f: f.name, reverse=True)
    except OSError:
        return 0
    deleted = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def save_job_output(job_id: str, output: str) -> Path:
    """Save job output to <cron>/output/<job_id>/<timestamp>.md and prune."""
    job_output_dir = _job_output_dir(job_id)
    job_output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _now().strftime("%Y-%m-%d_%H-%M-%S")
    output_file = job_output_dir / f"{timestamp}.md"
    fd, tmp_path = tempfile.mkstemp(dir=str(job_output_dir), suffix=".tmp",
                                    prefix=".output_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(output)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(Path(tmp_path), output_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _prune_job_output(job_output_dir, _cron_output_keep())
    return output_file


def job_output(job_id: str) -> str | None:
    """Most recent run output for a job, or None (context_from consumer)."""
    try:
        out_dir = _job_output_dir(job_id)
        if not out_dir.exists():
            return None
        files = sorted(out_dir.glob("*.md"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return None
        return files[0].read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


# ── context_from chaining (cron/scheduler.py _build_job_prompt) ─────────────
def context_from(prompt: str, refs, job_id: str = "") -> str:
    """Inject the most recent output of each referenced job into the prompt.

    Ported from scheduler.py ``_build_job_prompt``: upstream output is
    prepended as ``## Output from job '<id>'`` blocks (8K truncation per
    source), matching the Hermes chaining contract for job A -> job B
    pipelines. Invalid ids and unreadable outputs are silently skipped.
    """
    if not refs:
        return prompt
    if isinstance(refs, str):
        refs = [refs]
    prompt = str(prompt or "")
    for source_job_id in refs:
        # Path-traversal guard: valid job IDs are 12-char hex strings.
        if not source_job_id or not all(c in "0123456789abcdef"
                                        for c in source_job_id):
            continue
        try:
            latest_output = job_output(source_job_id)
        except Exception:
            continue
        if not latest_output:
            continue
        if len(latest_output) > _MAX_CONTEXT_CHARS:
            latest_output = latest_output[:_MAX_CONTEXT_CHARS] + "\n\n[... output truncated ...]"
        prompt = (
            f"## Output from job '{source_job_id}'\n"
            "The following is the most recent output from a preceding "
            "cron job. Use it as context for your analysis.\n\n"
            f"```\n{latest_output}\n```\n\n"
            f"{prompt}"
        )
    return prompt


# ── script execution (cron/scheduler.py _run_job_script) ────────────────────
def _run_job_script(script_path: str, workdir: str | None = None) -> tuple:
    """Execute a cron job's script and capture its output.

    Scripts must reside within <HERMES_HOME>/scripts/ (relative or
    absolute, both validated — path traversal is blocked). .sh/.bash run
    via bash, anything else via the current Python interpreter. The
    subprocess cwd is the job workdir when set, else the scripts dir
    parent; the Python process cwd is never mutated.

    Returns (success, output); on failure `output` carries the error text.
    """
    scripts_dir = detect.hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir_resolved = scripts_dir.resolve()

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()

    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return False, (
            f"Blocked: script path resolves outside the scripts directory "
            f"({scripts_dir_resolved}): {script_path!r}")

    if not path.exists():
        return False, f"Script not found: {path}"
    if not path.is_file():
        return False, f"Script path is not a file: {path}"

    script_timeout = _resolve_script_timeout()

    suffix = path.suffix.lower()
    if suffix in {".sh", ".bash"}:
        _bash = shutil.which("bash") or (
            "/bin/bash" if os.path.isfile("/bin/bash") else None)
        if _bash is None:
            return False, (
                f"Cannot run .sh/.bash script {path.name!r}: bash not found "
                f"on PATH. On Windows, install Git for Windows (which ships "
                f"Git Bash) or rewrite the script as Python (.py).")
        argv = [_bash, str(path)]
        env = dict(os.environ)
    else:
        argv = [sys.executable, str(path)]
        env = dict(os.environ)

    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
                      "encoding": "utf-8", "errors": "replace"}
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=script_timeout,
            cwd=workdir or str(path.parent), env=env, **kwargs)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            parts = [f"Script exited with code {result.returncode}"]
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            return False, "\n".join(parts)
        return True, stdout
    except subprocess.TimeoutExpired:
        return False, f"Script timed out after {script_timeout}s: {path}"
    except Exception as exc:
        return False, f"Script execution failed: {exc}"


def _parse_wake_gate(script_output: str) -> bool:
    """Honour a trailing ``wakeAgent=false`` marker as a silent signal
    (cron/scheduler.py _parse_wake_gate)."""
    try:
        tail = script_output.strip().splitlines()[-1].strip()
    except IndexError:
        return True
    if not tail:
        return True
    return tail.lower() != "wakeagent=false"


# ── run job (cron/scheduler.py run_job no_agent path) ───────────────────────
def run_job(job_id: str) -> dict:
    """Run a cron job now, synchronously. Returns {ok, output, error}.

    Script (``no_agent``) jobs execute their script and deliver its stdout
    verbatim; empty stdout / ``wakeAgent=false`` are silent successes,
    non-zero exit is a failure. Agent-mode jobs cannot run inside Atropos
    (no live gateway agent), so they are recorded as an error and reported
    — the job record and list stay honest.
    """
    job = get_job(job_id)
    if job is None:
        return {"ok": False, "error": f"Job not found: {job_id}"}
    if not job.get("enabled", True) or job.get("state") == "paused":
        return {"ok": False, "error": "Job is paused/disabled; resume it before running."}

    job_name = str(job.get("name") or job.get("prompt") or job_id or "cron job")
    now_iso = _now().strftime("%Y-%m-%d %H:%M:%S")

    if job.get("no_agent"):
        script_path = job.get("script")
        if not script_path:
            err = "no_agent=True but no script is set for this job"
            mark_job_run(job_id, False, err)
            return {"ok": False, "output": "", "error": err}

        _job_workdir = (job.get("workdir") or "").strip() or None
        if _job_workdir and not Path(_job_workdir).is_dir():
            _job_workdir = None

        ok, output = _run_job_script(script_path, workdir=_job_workdir)

        if not ok:
            alert = (f"⚠ Cron watchdog '{job_name}' script failed\n\n"
                     f"{output}\n\nTime: {now_iso}")
            doc = (f"# Cron Job: {job_name}\n\n"
                   f"**Job ID:** {job_id}\n"
                   f"**Run Time:** {now_iso}\n"
                   f"**Mode:** no_agent (script)\n"
                   f"**Status:** script failed\n\n{output}\n")
            save_job_output(job_id, doc)
            mark_job_run(job_id, False, output)
            return {"ok": False, "output": doc, "error": output}

        if not _parse_wake_gate(output):
            silent_doc = (f"# Cron Job: {job_name}\n\n"
                          f"**Job ID:** {job_id}\n"
                          f"**Run Time:** {now_iso}\n"
                          f"**Mode:** no_agent (script)\n"
                          f"**Status:** silent (wakeAgent=false)\n")
            save_job_output(job_id, silent_doc)
            mark_job_run(job_id, True)
            return {"ok": True, "output": "", "error": None, "silent": True}

        if not output.strip():
            silent_doc = (f"# Cron Job: {job_name}\n\n"
                          f"**Job ID:** {job_id}\n"
                          f"**Run Time:** {now_iso}\n"
                          f"**Mode:** no_agent (script)\n"
                          f"**Status:** silent (empty output)\n")
            save_job_output(job_id, silent_doc)
            mark_job_run(job_id, True)
            return {"ok": True, "output": "", "error": None, "silent": True}

        doc = (f"# Cron Job: {job_name}\n\n"
               f"**Job ID:** {job_id}\n"
               f"**Run Time:** {now_iso}\n"
               f"**Mode:** no_agent (script)\n\n---\n\n{output}\n")
        save_job_output(job_id, doc)
        mark_job_run(job_id, True)
        return {"ok": True, "output": doc, "error": None}

    # Agent path — not runnable in Atropos (no live gateway agent). Record
    # the failure so the job does not sit forever with a stale status.
    err = ("Cannot run agent-mode cron job: Atropos has no live gateway "
           "agent. Run this job inside Hermes, or recreate it with "
           "no_agent=True and a script.")
    mark_job_run(job_id, False, err)
    return {"ok": False, "output": "", "error": err}


# ── due jobs + ticker (cron/jobs.py get_due_jobs) ───────────────────────────
def get_due_jobs(now: float | None = None) -> list:
    """Jobs whose next_run_at has arrived and are enabled and not paused."""
    now = _now() if now is None else datetime.fromtimestamp(now, tz=_now().tzinfo)
    due = []
    for job in load_jobs():
        if not job.get("enabled", True) or job.get("state") == "paused":
            continue
        raw = job.get("next_run_at")
        if not raw:
            continue
        try:
            nxt = _ensure_aware(datetime.fromisoformat(raw))
        except ValueError:
            continue
        if nxt <= now:
            due.append(_normalize_job_record(job))
    return due


def tick(now: float | None = None) -> list:
    """One scheduler tick: run every due job once. Returns run results.

    This is the Atropos ticker — a plain function callers (watch loops,
    ``atropos cron`` CLI) invoke on their own schedule. Recurring jobs are
    advanced by mark_job_run; a crash between due-check and run cannot
    double-fire more than once per due interval.
    """
    results = []
    for job in get_due_jobs(now):
        results.append({"job_id": job["id"], **run_job(job["id"])})
    return results


def record_ticker_heartbeat(success: bool = False) -> None:
    """Record ticker liveness signals (cron/jobs.py): epoch markers in the
    cron dir so external status checks can distinguish alive from failing."""
    try:
        _write_text_atomic(_cron_dir() / "ticker_heartbeat", str(time.time()))
    except Exception:
        pass
    if success:
        try:
            _write_text_atomic(_cron_dir() / "ticker_last_success", str(time.time()))
        except Exception:
            pass


def get_ticker_heartbeat_age() -> float | None:
    """Seconds since the ticker loop last iterated, or None if unknown."""
    try:
        raw = (_cron_dir() / "ticker_heartbeat").read_text(encoding="utf-8").strip()
        return max(0.0, time.time() - float(raw))
    except Exception:
        return None


# ── display / formatting (cronjob_tools.py _format_job / _repeat_display) ───
def _repeat_display(job: dict) -> str:
    times = (job.get("repeat") or {}).get("times")
    completed = (job.get("repeat") or {}).get("completed", 0)
    if times is None:
        return "forever"
    if times == 1:
        return "once" if completed == 0 else "1/1"
    return f"{completed}/{times}" if completed else f"{times} times"


def format_job(job: dict) -> dict:
    """Tool-facing job shape (cronjob_tools.py _format_job)."""
    prompt = str(job.get("prompt") or "")
    skills = _canonical_skills(job.get("skill"), job.get("skills"))
    job_id = str(job.get("id") or "unknown")
    name = str(job.get("name") or prompt[:50] or (skills[0] if skills else "")
               or job_id or "cron job")
    result = {
        "job_id": job_id,
        "name": name,
        "skill": skills[0] if skills else None,
        "skills": skills,
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        "model": job.get("model"),
        "provider": job.get("provider"),
        "base_url": job.get("base_url"),
        "schedule": job.get("schedule_display") or "?",
        "repeat": _repeat_display(job),
        "deliver": job.get("deliver", "local"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_delivery_error": job.get("last_delivery_error"),
        "enabled": job.get("enabled", True),
        "state": job.get("state", "scheduled" if job.get("enabled", True) else "paused"),
        "paused_at": job.get("paused_at"),
        "paused_reason": job.get("paused_reason"),
    }
    if job.get("script"):
        result["script"] = job["script"]
    if job.get("no_agent"):
        result["no_agent"] = True
    if job.get("enabled_toolsets"):
        result["enabled_toolsets"] = job["enabled_toolsets"]
    if job.get("workdir"):
        result["workdir"] = job["workdir"]
    return result


# ── yaml sidecar jobs (legacy store, dashboard.api_cron format) ─────────────
def _yaml_jobs() -> list:
    """Jobs from <HERMES_HOME>/cron/*.yaml (the legacy store the dashboard
    still reads): {job_id: stem, name, file, schedule, command, enabled}."""
    cron_dir = _cron_dir()
    out = []
    if not cron_dir.is_dir():
        return out
    for f in sorted(list(cron_dir.glob("*.yaml")) + list(cron_dir.glob("*.yml"))):
        try:
            data = config.parse_yaml(f.read_text(encoding="utf-8"))
            out.append({
                "job_id": f.stem,
                "name": data.get("name", f.stem),
                "file": f.name,
                "schedule": data.get("schedule", data.get("cron", "")),
                "command": data.get("command", data.get("task", "")),
                "enabled": data.get("enabled", True),
                "yaml": True,
            })
        except Exception:
            out.append({"job_id": f.stem, "name": f.stem, "file": f.name,
                        "schedule": "", "command": "", "enabled": True,
                        "yaml": True, "error": "unparsable"})
    return out


def get_due_yaml_jobs(now_ts: float | None = None) -> list:
    """Legacy yaml jobs that are enabled and whose schedule came due.

    ``next_run`` is computed on the fly (the yaml store keeps no run
    state); a yaml job is due when its next run lands within the last
    tick interval. ``command`` is a shell line; Atropos records the run
    in cron_state.json (the dashboard's store) instead of executing
    arbitrary shell from yaml files.
    """
    now = _now() if now_ts is None else datetime.fromtimestamp(now_ts, tz=_now().tzinfo)
    due = []
    for j in _yaml_jobs():
        if not j.get("enabled", True):
            continue
        spec_text = j.get("schedule") or ""
        if not spec_text:
            continue
        try:
            spec = parse_schedule(spec_text)
        except ValueError:
            continue
        if spec.get("kind") == "once":
            continue  # one-shot yaml jobs need persisted state; not executed here
        nxt = next_run(spec)
        if nxt is None:
            continue
        nxt_dt = datetime.fromtimestamp(nxt, tz=_now().tzinfo)
        if now - timedelta(seconds=_TICKER_INTERVAL_SECONDS) <= nxt_dt <= now:
            due.append(j)
    return due
