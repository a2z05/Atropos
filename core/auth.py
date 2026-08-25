"""Dashboard password auth — first-run setup, PBKDF2 hash, cookie sessions.

Replaces the old auto-generated ``auth_token`` file: the dashboard is
unusable until the owner sets a password on first open (``needs_setup``),
then every request authenticates via an opaque session cookie issued at
login. Nothing is stored in plaintext:

* the password lives only as PBKDF2-HMAC-SHA256 (200k iterations) with a
  per-password salt in ``~/.atropos/dashboard_auth.json``
* sessions are random tokens (``secrets.token_urlsafe``) persisted next to
  the hash so restarts / redeploys don't log the browser out
* login attempts are rate-limited (5 failures -> 30s cooldown) because the
  dashboard may bind a public interface (Railway)

Legacy migration: a password previously saved in the plaintext setting
``dashboard.password`` is accepted once, then re-hashed into the store and
removed from config.yaml.

Stdlib-only; safe to import from any core module.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from . import detect

# PBKDF2 parameters — 200k iterations is the OWASP-recommended floor for
# SHA-256 as of 2023 and costs ~100ms, invisible on login but brutal for
# offline brute force.
PBKDF2_ITERATIONS = 200_000

_AUTH_FILE = "dashboard_auth.json"
_COOKIE_NAME = "atropos_session"
_SESSION_TTL = 30 * 24 * 3600  # 30 days
_MAX_SESSIONS = 20             # prune oldest beyond this
_MAX_LOGIN_FAILURES = 5
_LOCKOUT_SECONDS = 30.0

_LOCK = threading.RLock()

# process-local rate-limit state (attempts since last success + lockout ts)
_failures = 0
_lockout_until = 0.0


def _auth_path() -> Path:
    return detect.atropos_home() / _AUTH_FILE


def _load_store() -> dict:
    p = _auth_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # corrupt store == no credentials; first-run setup will recreate it
        return {}


def _save_store(store: dict):
    p = _auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")
    try:
        os_chmod_private(p)
    except Exception:
        pass


def os_chmod_private(p: Path):
    """Best-effort restrictive permissions (no-op where unsupported)."""
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass


# ── hashing ───────────────────────────────────────────────────────────────
def _hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {
        "algo": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "hash": dk.hex(),
    }


def _verify_password(password: str, entry: dict) -> bool:
    """Constant-time verify against a stored hash entry."""
    try:
        salt = bytes.fromhex(entry["salt"])
        expected = bytes.fromhex(entry["hash"])
        iterations = int(entry.get("iterations", PBKDF2_ITERATIONS))
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, iterations)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ── state ─────────────────────────────────────────────────────────────────
def needs_setup() -> bool:
    """True when no password exists yet — dashboard shows the create form."""
    with _LOCK:
        return not _load_store().get("password")


def set_password(password: str) -> dict:
    """First-run (or explicit reset): write the hashed password + empty sessions."""
    if not isinstance(password, str) or len(password) < 4:
        return {"ok": False, "error": "password must be at least 4 characters"}
    if len(password) > 1024:
        return {"ok": False, "error": "password too long (max 1024)"}
    with _LOCK:
        store = _load_store()
        store["password"] = _hash_password(password)
        store["sessions"] = {}
        store["legacy_checked"] = True
        _save_store(store)
    return {"ok": True}


def change_password(old: str, new: str) -> dict:
    """Rotate the password; invalidates all existing sessions."""
    with _LOCK:
        store = _load_store()
        entry = store.get("password")
        if not entry:
            return {"ok": False, "error": "no password set"}
        if not verify_rate_limited(old)[0]:
            return {"ok": False, "error": "current password incorrect"}
        res = set_password(new)
        return res


def _check_legacy_setting(password: str) -> bool:
    """Accept a legacy plaintext dashboard.password once, then migrate it
    into the hashed store so the plaintext copy leaves config.yaml."""
    from . import settings
    try:
        legacy = settings.get("dashboard.password", "") or ""
    except Exception:
        return False
    if not legacy:
        return False
    if hmac.compare_digest(password, legacy):
        with _LOCK:
            store = _load_store()
            store["password"] = _hash_password(legacy)
            store.setdefault("sessions", {})
            store["legacy_checked"] = True
            _save_store(store)
            # scrub the plaintext out of config.yaml
            try:
                settings.set("dashboard.password", "")
            except Exception:
                pass
        return True
    return False


# ── rate limiting ─────────────────────────────────────────────────────────
def _rate_limited() -> bool:
    global _failures, _lockout_until
    now = time.monotonic()
    if _lockout_until > now:
        return True
    if _failures >= _MAX_LOGIN_FAILURES:
        _lockout_until = now + _LOCKOUT_SECONDS
        _failures = 0
        return True
    return False


def verify_rate_limited(password: str) -> tuple[bool, float | None]:
    """Verify a login attempt under the failure lockout.

    Returns (ok, retry_after_seconds_or_None). The lockout applies to the
    whole process (single-owner deployment); a successful attempt resets it.
    """
    global _failures, _lockout_until
    with _LOCK:
        if isinstance(password, str) and _rate_limited():
            wait = round(_lockout_until - time.monotonic(), 1)
            return False, max(wait, 0.1)
        store = _load_store()
        entry = store.get("password")
        ok = bool(entry) and _verify_password(password, entry)
        if not ok:
            # also accept a legacy plaintext dashboard.password (migrates
            # it into the hashed store on success); runs even when no
            # hashed entry exists yet — that's exactly the migration case
            ok = _check_legacy_setting(password)
        if ok:
            _failures = 0
            _lockout_until = 0.0
        else:
            _failures += 1
        return ok, None


# ── sessions ──────────────────────────────────────────────────────────────
def create_session() -> str:
    """Mint a new session token after a successful login."""
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    with _LOCK:
        store = _load_store()
        sessions = store.setdefault("sessions", {})
        sessions[tok] = {"created": now, "last_seen": now}
        # prune oldest beyond the cap
        if len(sessions) > _MAX_SESSIONS:
            for k, _v in sorted(sessions.items(), key=lambda kv: kv[1].get("created", 0))[
                    :len(sessions) - _MAX_SESSIONS]:
                sessions.pop(k, None)
        _save_store(store)
    return tok


def validate_session(token: str | None) -> bool:
    """True when the cookie token maps to a live session (touch last_seen)."""
    if not token:
        return False
    now = int(time.time())
    with _LOCK:
        store = _load_store()
        sess = store.get("sessions", {}).get(token)
        if not sess:
            return False
        if now - int(sess.get("created", 0)) > _SESSION_TTL:
            store.get("sessions", {}).pop(token, None)
            _save_store(store)
            return False
        if now - int(sess.get("last_seen", 0)) > 3600:
            sess["last_seen"] = now
            _save_store(store)
        return True


def drop_session(token: str | None):
    """Logout: forget one session token."""
    if not token:
        return
    with _LOCK:
        store = _load_store()
        if token in store.get("sessions", {}):
            store["sessions"].pop(token, None)
            _save_store(store)


def drop_all_sessions():
    """Password change/reset: every browser must log in again."""
    with _LOCK:
        store = _load_store()
        if store.get("sessions"):
            store["sessions"] = {}
            _save_store(store)


# ── machine tokens (fleet/sync peers, non-browser clients) ────────────────
def machine_token() -> str:
    """Stable API token for non-cookie callers (livesync peers).

    Replaces the old shared auth_token file; kept separate from browser
    sessions so rotating the password never breaks peer sync.
    """
    with _LOCK:
        store = _load_store()
        tok = store.get("machine_token")
        if not tok:
            tok = secrets.token_urlsafe(24)
            store["machine_token"] = tok
            _save_store(store)
        return tok
