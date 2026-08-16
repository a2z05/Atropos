#!/usr/bin/env python3
"""Atropos AI Update Engine — stdlib only.

Activated ONLY when a patch fails to apply during an update. Given an
upstream-new version, the current version, and the failed patch (which patch /
which line / why — API renamed? path moved? file deleted?), it rewrites the
patch for the new version, runs tests + doctor, shows a diff, requires
confirmation, then applies. If it cannot fix, it produces a human-facing report.

The "AI" step is pluggable: ``llm_rewrite`` is a module-level hook the harness
can patch/mock. The default ``_fallback_rewrite`` is fully deterministic and
offline so tests + no-config runs always succeed.

NEVER requires a network call in tests.
"""
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, detect, doctor, patches, settings

HISTORY_FILE = "update_ai.json"

_STOP_WORDS = {
    "if", "not", "and", "or", "return", "self", "msg", "in", "is", "for",
    "while", "def", "class", "import", "from", "as", "try", "except", "raise",
    "else", "elif", "pass", "with", "lambda", "none", "true", "false", "the",
    "to", "of", "it", "be", "this", "that", "new", "old", "get", "set", "call",
}


# ── history store ──────────────────────────────────────────────────────────
def _history_path() -> Path:
    return detect.atropos_home() / HISTORY_FILE


def load_history() -> dict:
    """Load the update_ai.json history. Returns {attempts: [...]}."""
    p = _history_path()
    if not p.exists():
        return {"attempts": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "attempts" not in data:
            data = {"attempts": []}
        if not isinstance(data["attempts"], list):
            data["attempts"] = []
    except Exception:
        data = {"attempts": []}
    return data


def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ts_micro():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def append_attempt(record: dict) -> dict:
    """Append an attempt to update_ai.json with a collision-proof id.

    The id is ``ts(microsecond) + short-hash(patch_id|upstream|current)``, so
    two attempts for the same patch + versions within the same instant still
    get distinct, deterministic-but-unique ids. The persisted ``ts`` keeps
    second granularity for human display.
    """
    hist = load_history()
    ts_full = _ts_micro()  # always fresh → collision-proof
    ts = record.get("ts") or ts_full[:15]
    patch_id = record.get("patch_id", "")
    uv = record.get("upstream_version", "")
    cv = record.get("current_version", "")
    rid = f"{ts_full}-{_short_hash(patch_id + '|' + uv + '|' + cv)}"
    attempt = {
        "id": rid,
        "ts": ts,
        "upstream_version": uv,
        "current_version": cv,
        "patch_id": patch_id,
        "status": record.get("status", "running"),
        "mode": record.get("mode"),
        "model": record.get("model"),
        "effort": record.get("effort"),
        "diff": record.get("diff"),
        "error": record.get("error"),
        "notes": record.get("notes"),
        "rewritten_patch": record.get("rewritten_patch"),
        "engine": record.get("engine"),
    }
    hist["attempts"].append(attempt)
    _history_path().write_text(json.dumps(hist, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    return attempt


def update_attempt(attempt_id: str, **fields) -> dict:
    """Patch fields onto an existing attempt and persist. Returns it (or None)."""
    hist = load_history()
    for a in hist["attempts"]:
        if a.get("id") == attempt_id:
            a.update(fields)
            _history_path().write_text(json.dumps(hist, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
            return a
    return None


# ── text helpers ────────────────────────────────────────────────────────────
def peek_lines(text: str) -> list:
    """Tokenize source text into stable search anchors.

    Lowercased, whitespace-collapsed non-empty lines, deduplicated while
    preserving order — used for anchor-presence comparisons.
    """
    seen = []
    for line in text.splitlines():
        tok = " ".join(line.strip().lower().split())
        if tok and tok not in seen:
            seen.append(tok)
    return seen


def _tokenize_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^\|\|?-", "", line)
    line = re.sub(r"^[0-9]+[a-z]?\s", "", line)
    line = re.sub(r"\s+", " ", line)
    return " ".join(line.lower().split())


def _extract_symbols(line: str) -> set:
    """Identifiers (with underscores) from a line, minus noise words."""
    syms = set()
    for m in re.finditer(r"[a-zA-Z_][a-zA-Z0-9_]*", line):
        s = m.group(0)
        if s.lower() in _STOP_WORDS or len(s) < 3:
            continue
        syms.add(s)
    return syms


def _lcs_len(a: str, b: str) -> int:
    """Longest common subsequence length; small strings so DP is fine."""
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            if a[i] == b[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    return dp[0][0]


def _symbol_similarity(a: str, b: str) -> float:
    aa = re.sub(r"[^a-z0-9]", "", a.lower())
    bb = re.sub(r"[^a-z0-9]", "", b.lower())
    if not aa or not bb:
        return 0.0
    return _lcs_len(aa, bb) / max(len(aa), len(bb))


def _symbol_rename(sym: str, up_syms) -> str:
    """Best-case candidate for ``sym`` among upstream symbols."""
    best, best_score = "", 0.0
    for us in up_syms:
        if us == sym:
            continue
        s = _symbol_similarity(sym, us)
        if s > best_score:
            best, best_score = us, s
    return best if best_score >= 0.5 else ""


# ── patch file access ───────────────────────────────────────────────────────
def _patch_file(patch_id: str):
    """Return (path, data) for the hack YAML with this id, or (None, None)."""
    if not patch_id:
        return None, None
    for f in sorted(patches.HACKS_DIR.glob("*.yml")):
        try:
            data = config.parse_yaml(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("id") == patch_id:
            return f, data
    return None, None


def _patch_text(patch_id: str) -> str:
    f, data = _patch_file(patch_id)
    return f.read_text(encoding="utf-8") if f else ""


def _hack_path(patch_id: str) -> Path:
    f, _ = _patch_file(patch_id)
    return f if f else patches.HACKS_DIR / "update-ai-rewritten.yml"


def _target_rel(patch_id: str) -> str:
    _, data = _patch_file(patch_id)
    return data.get("target", "plugins/platforms/telegram/adapter.py") if data else \
        "plugins/platforms/telegram/adapter.py"


def _repo_root() -> Path:
    return Path(patches.HACKS_DIR).resolve().parent


def _detect_repo() -> Path:
    return _repo_root()


# ── diagnosis (deterministic, rule-based, no LLM) ───────────────────────────
def _patch_anchors(patch_id):
    """(anchors, target, patch_text) from the hack file for patch_id."""
    f, data = _patch_file(patch_id)
    if not f:
        return None, None, ""
    old = data.get("old") or ""
    anchors = [line for line in old.splitlines() if line.strip()]
    return anchors, data.get("target"), f.read_text(encoding="utf-8")


def _differential_anchors(current: str, upstream: str) -> list:
    """Case-preserved lines present in current but absent upstream — implicit
    search anchors of the failed patch when its YAML is unavailable."""
    up_tokens = set(peek_lines(upstream))
    anchors = []
    for line in current.splitlines():
        tok = _tokenize_line(line)
        if tok and tok not in up_tokens and line not in anchors:
            anchors.append(line)
    return anchors


def _path_status(rel: str, upstream: str) -> str:
    if rel in upstream:
        return "present"
    has_paths = any("/" in t for t in upstream.split())
    if not has_paths:
        return "unknown"
    base = Path(rel).name
    if base and base in upstream.split():
        return "moved"
    return "deleted"


def diagnose_failure(patch_id, current_source_text, upstream_source_text, error=None) -> dict:
    """Conservative, deterministic failure analysis for a failed patch.

    Compares which search anchors of the patch exist in the current (old)
    source vs the upstream (new) source (identifier-level, case-preserving):

      * anchor present in current but renamed upstream (similar identifier) →
        api_renamed, with machine-readable old_token/rename_candidate,
      * the patch's ``target`` path is gone / moved upstream → file_deleted /
        path_moved,
      * anchor present in current but gone upstream with no rename match →
        conflict,
      * anchor renamed *and* the file reshaped → format_changed too.

    Returns {patch_id, reason_categories, details, suggested_action}.
    """
    current = current_source_text or ""
    upstream = upstream_source_text or ""
    details = []
    categories = []

    patch_anchors, target, patch_text = _patch_anchors(patch_id)
    cur_tokens = set(peek_lines(current))
    up_tokens = set(peek_lines(upstream))

    if patch_anchors:
        anchors = patch_anchors
    else:
        anchors = _differential_anchors(current, upstream)
        if not anchors:
            details.append("patch contributes no search anchors and the sources "
                           "are identical where it applies")
            result = {"patch_id": patch_id, "reason_categories": ["unknown"],
                      "details": details, "suggested_action": "manual review required"}
            if patch_text:
                result["patch_text"] = patch_text
            return result

    if target:
        rel = target.lstrip("/")
        status = _path_status(rel, upstream)
        if status == "deleted":
            categories.append("file_deleted")
            details.append(f"target path {rel!r} deleted upstream")
        elif status == "moved":
            categories.append("path_moved")
            details.append(f"target path {rel!r} moved upstream")

    failed = []
    up_all_syms = set()
    for line in upstream.splitlines():  # original lines keep identifier underscores
        up_all_syms |= _extract_symbols(line)
    for anchor in anchors:
        tok = _tokenize_line(anchor)
        if tok in cur_tokens and tok not in up_tokens:
            failed.append(anchor)
    rename_pair = None
    if failed:
        best = None
        best_score = 0.0
        for anchor in failed:
            for sym in _extract_symbols(anchor):
                cand = _symbol_rename(sym, up_all_syms)
                if cand and _symbol_similarity(sym, cand) > best_score:
                    best = (sym, cand)
                    best_score = _symbol_similarity(sym, cand)
        if best:
            rename_pair = best
            categories.append("api_renamed")
            details.append(f"anchors renamed upstream ({len(failed)} failed, "
                           f"e.g. {best[0]!r} → {best[1]!r})")
        else:
            categories.append("conflict")
            details.append(f"anchors gone upstream with no rename candidate: "
                           f"{[re.sub(r'\\s+', ' ', a)[:48] for a in failed[:4]]}")
    if categories and _heavily_reshaped(current, upstream):
        categories.append("format_changed")
        details.append("file heavily reshaped upstream (format/indentation drift)")

    if not categories:
        if set(peek_lines(current)) == set(peek_lines(upstream)):
            categories.append("unknown")
            details.append("current and upstream sources are identical — no "
                           "differential signal to diagnose")
        else:
            categories.append("conflict")
            details.append("no anchor differences detected — patch conflict assumed")

    canonical = ["api_renamed", "path_moved", "file_deleted", "format_changed",
                 "conflict", "unknown"]
    seen = set()
    reason_categories = [c for c in canonical
                         if c in categories and not (c in seen or seen.add(c))]
    if not reason_categories:
        reason_categories = ["unknown"]

    result = {
        "patch_id": patch_id,
        "reason_categories": reason_categories,
        "details": details,
        "suggested_action": _suggest(reason_categories,
                                     rename_pair[1] if rename_pair else None),
    }
    if rename_pair:
        result["old_token"] = rename_pair[0]
        result["rename_candidate"] = rename_pair[1]
    if patch_text:
        result["patch_text"] = patch_text
    return result


def _heavily_reshaped(current: str, upstream: str) -> bool:
    if len(upstream) < 400:
        return False
    return (len(upstream) - len(current)) / len(upstream) > 0.33


def _suggest(reasons: list, rename_candidate) -> str:
    if "api_renamed" in reasons:
        r = "rename / re-anchor the patch to the new upstream API"
        if rename_candidate:
            r += f" (closest upstream match: {rename_candidate!r})"
        return r
    if "path_moved" in reasons:
        return "re-target the patch to the moved path upstream"
    if "file_deleted" in reasons:
        return "the patched file was removed upstream — review whether the hack is still needed"
    if "format_changed" in reasons:
        return "re-diff the patch against the reformatted upstream file"
    if "conflict" in reasons:
        return "manual conflict resolution required"
    return "manual review required"


# ── rewrite (the AI step) ────────────────────────────────────────────────────
_LLM_PROVIDER = None  # harness-set callable; None = no real LLM configured


def _settings_mode() -> str:
    return settings.get("update-ai.mode", "manual")


def mode_gate(action: str) -> bool:
    """update-ai.mode gate.

    * ``off``  → blocks everything (False),
    * ``manual`` → allows analyze/preview, but requires confirm for apply,
    * ``auto``  → allows confirm-gated apply automatically only for
      non-conflicting rewrites.
    """
    mode = _settings_mode()
    if mode == "off":
        return False
    if action in ("analyze", "preview"):
        return mode in ("manual", "auto")
    if action == "apply":
        return mode == "auto"
    return False


def _mode():
    return _settings_mode()


def llm_rewrite(patch_text, context) -> str:
    """THE AI step. Pluggable module-level hook — the harness wires this to a
    real provider later; tests/mocks replace it. Returns rewritten patch text."""
    if callable(_LLM_PROVIDER):
        return _LLM_PROVIDER(patch_text, context)
    raise RuntimeError("no LLM provider configured — use the fallback engine")


def _replace_token(text: str, old: str, new: str) -> str:
    """Replace the identifier ``old`` with ``new`` where it stands alone
    (not a substring of a longer identifier)."""
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old) + r"(?![A-Za-z0-9_])")
    return pat.sub(re.sub(r"[^A-Za-z0-9_]", "", new), text)


def _fallback_rewrite(patch_text, diagnosis) -> str:
    """Deterministic mechanical rewrite of a patch YAML from the diagnosis.

    * api_renamed → substitute the diagnosis's rename candidate for the
      failing identifier across old/new blocks,
    * path issues → return the patch unchanged (manual),
    * nothing to do → return the patch unchanged.
    """
    out = patch_text or ""
    reasons = diagnosis.get("reason_categories", [])
    if "api_renamed" in reasons:
        old_tok = diagnosis.get("old_token", "")
        new_tok = diagnosis.get("rename_candidate", "")
        if old_tok and new_tok and old_tok != new_tok:
            out = _replace_token(out, old_tok, new_tok)
    return out


def rewrite_patch(patch_id, current, upstream, diagnosis, model=None, effort=None) -> dict:
    """The rewrite step. Returns a dict with the rewritten patch.

    * mode ``off`` → {"ok": False, "reason": "mode off"} without calling anything.
    * No real LLM configured (model empty or no provider) → fallback engine,
      "engine": "fallback".
    * Real LLM configured (model non-empty AND a callable provider) →
      "engine": "llm" and llm_rewrite is called (network may be used).
    """
    if not mode_gate("analyze"):
        return {"ok": False, "reason": "mode off", "mode": _mode()}
    model = model if model is not None else settings.get("update-ai.model", "")
    effort = effort if effort is not None else settings.get("update-ai.effort", "medium")

    work = dict(diagnosis or {})
    patch_text = work.get("patch_text") or _patch_text(patch_id)
    work["patch_text"] = patch_text

    if model and callable(_LLM_PROVIDER):
        engine = "llm"
        try:
            text = llm_rewrite(patch_text, {
                "patch_id": patch_id,
                "diagnosis": diagnosis,
                "current": current,
                "upstream": upstream,
                "model": model,
                "effort": effort,
            })
            rewritten = (text or patch_text) or ""
        except Exception as e:
            return {"ok": False, "reason": "llm rewrite failed",
                    "engine": engine, "error": str(e),
                    "mode": _mode(), "model": model, "effort": effort}
    else:
        engine = "fallback"
        rewritten = _fallback_rewrite(patch_text, work)

    changed = rewritten.strip() != patch_text.strip()
    return {
        "ok": True,
        "engine": engine,
        "rewritten_patch": rewritten,
        "changed": changed,
        "mode": _mode(),
        "model": model,
        "effort": effort,
        "conflicts": "conflict" in (diagnosis or {}).get("reason_categories", []),
    }


# ── doctor + tests ───────────────────────────────────────────────────────────
def doctor_checks(relevant=None) -> list:
    """Run core.doctor.doctor() and filter relevant checks."""
    try:
        checks = list(doctor.doctor(fix=False))
    except Exception as e:
        return [{"name": "doctor", "ok": False, "msg": str(e), "fixed": False}]
    if not relevant:
        return checks
    return [c for c in checks if c["name"] in relevant]


def relevant_doctor_checks() -> list:
    return ["patches", "python >= 3.10"]


def _run_tests(repo: Path, timeout=180):
    """Run the repo's test suite with a timeout. Returns subprocess-like result.

    Guards against running tests when the repo isn't the Atropos repo.
    """
    if not repo.exists():
        return _fake_result(fail=False, stderr="no repo")
    marker = (repo / "core" / "patches.py").exists() and (repo / "hacks").exists()
    if not marker:
        return _fake_result(fail=False, stderr="not an Atropos repo — tests skipped")
    try:
        return subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s",
             str(repo / "tests")],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fake_result(fail=True, stderr="test timeout after 180s")
    except Exception as e:
        return _fake_result(fail=True, stderr=str(e))


def _fake_result(fail: bool, stderr: str = ""):
    err = stderr
    class _R:
        returncode = 1 if fail else 0
        stdout = ""
        stderr = err
    return _R()


def tests_pass(result) -> bool:
    """Nonzero exit → failure (rollback trigger)."""
    return getattr(result, "returncode", 1) == 0


# ── preview ──────────────────────────────────────────────────────────────────
def failed_patch_state() -> dict:
    """Build the state dict for ai_check() from the update system.

    Uses the last update apply/check result (update_state.json) plus the
    repo's upstream-vs-current sources for each conflicting hack. Empty
    when there is nothing to diagnose.
    """
    from . import detect as _d
    home = _d.atropos_home()
    state_path = home / "update_state.json"
    state = {"failed_patches": [], "current_version": "", "upstream_version": ""}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return state
    state["current_version"] = str(data.get("head", "") or "")
    state["upstream_version"] = str(data.get("remote", "") or "")
    conflicts = data.get("dry_run", {}).get("conflicts", []) if isinstance(
        data.get("dry_run"), dict) else []
    repo = _d.hermes_agent()
    for c in conflicts:
        pid = c.get("id", "")
        target = c.get("target") or _target_rel(pid)
        cur = up = ""
        if repo:
            cur = _git_show(repo, "HEAD:" + target)
            up = _git_show(repo, "origin/main:" + target)
        state["failed_patches"].append({
            "patch_id": pid, "target": target, "current_source": cur,
            "upstream_source": up, "error": c.get("reason", ""),
        })
    return state


def _git_show(repo: Path, ref: str) -> str:
    """git show <ref> output ('' on failure)."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(repo), "show", ref],
                           capture_output=True, timeout=15)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
    except Exception:
        return ""


def ai_check(state: dict) -> dict:
    """Diagnose + rewrite the pending failed patches. Preview only — no apply.

    ``state``: {upstream_version, current_version,
                failed_patches: [{patch_id, target, current_source,
                                  upstream_source, error}]}
    Each preview: {attempt, diagnosis, rewritten_patch, diff, tests}.
    """
    if not mode_gate("analyze"):
        return {"ok": False, "reason": "mode off", "previews": []}
    uv = state.get("upstream_version", "")
    cv = state.get("current_version", "")
    upstream = state.get("upstream")
    previews = []
    for fp in state.get("failed_patches", []):
        pid = fp.get("patch_id", "")
        current_src = fp.get("current_source") or state.get("current")
        upstream_src = fp.get("upstream_source") or upstream
        error = fp.get("error")
        diagnosis = diagnose_failure(pid, current_src, upstream_src, error=error)
        rewrite = rewrite_patch(pid, cv, uv, diagnosis)
        notes = "; ".join(diagnosis.get("details", []))
        attempt = append_attempt({
            "patch_id": pid,
            "upstream_version": uv,
            "current_version": cv,
            "status": "suggested",
            "mode": rewrite.get("mode", _mode()),
            "model": rewrite.get("model"),
            "effort": rewrite.get("effort"),
            "diff": _diff_text(diagnosis.get("patch_text", ""),
                               rewrite.get("rewritten_patch", "")),
            "notes": notes or None,
        })
        previews.append({
            "attempt": attempt,
            "diagnosis": diagnosis,
            "rewritten_patch": rewrite.get("rewritten_patch"),
            "diff": _diff_text(diagnosis.get("patch_text", ""),
                               rewrite.get("rewritten_patch", "")),
            "tests": {"run": False, "ok": None,
                      "note": "not run in preview (confirm to apply)"},
        })
    return {"ok": True, "upstream_version": uv, "current_version": cv,
            "previews": previews}


def _diff_text(old: str, new: str) -> str:
    """Minimal unified-ish diff of two strings (stdlib only)."""
    if not old:
        return f"+{new}" if new else ""
    if old == new:
        return "(unchanged)"
    old_lines, new_lines = old.splitlines(), new.splitlines()
    import difflib
    return "\n".join(difflib.unified_diff(old_lines, new_lines, "patch", "rewrite",
                                          lineterm=""))


# ── apply + rollback ─────────────────────────────────────────────────────────
def _write_patch(hack_path: Path, text: str):
    hack_path.parent.mkdir(parents=True, exist_ok=True)
    hack_path.write_text(text, encoding="utf-8")


def apply_ai(attempt_id, confirm=True, repo=None, timeout=180) -> dict:
    """Confirmation-gated apply of the rewritten patch pipeline.

    Phase 1 (dry-run, safe pre-confirm):
      * mode off → {"ok": False, "reason": "mode off"}
      * no rewritten patch for the attempt → {"ok": False, ...}
      * tests already failing at baseline → abort (a test failure before the
        rewrite is a rollback trigger, not something the engine should mask)
      * not confirmed (confirm False) → {"ok": False, "reason": "not confirmed"}

    Phase 2 (side-effecting apply):
      * backup previous patch YAML + reset the target to pristine via
        patches.apply_hacks(write=False)
      * write the rewritten patch to hacks/, run tests + doctor
      * on failure → rollback (restore previous patch, re-apply original
        hacks), status rolled_back.
    """
    rec = next((a for a in load_history()["attempts"]
                if a.get("id") == attempt_id), None)
    if rec is None:
        return {"ok": False, "reason": f"unknown attempt: {attempt_id}"}
    mode = _settings_mode()
    if mode == "off":
        return {"ok": False, "reason": "mode off", "attempt_id": attempt_id}
    patch_id = rec.get("patch_id", "")
    rewritten = (rec.get("rewritten_patch") or "").strip()
    if not rewritten:
        return {"ok": False, "reason": "no rewritten patch for attempt",
                "attempt_id": attempt_id}
    if rec.get("status") == "applied":
        return {"ok": True, "already": True, "attempt_id": attempt_id,
                "status": "applied"}

    repo = Path(repo) if repo else _detect_repo()
    hack_path = _hack_path(patch_id)

    tests_result = _run_tests(repo, timeout=timeout)
    if not tests_pass(tests_result):
        return {
            "ok": False, "reason": "tests fail at baseline — refusing to apply",
            "attempt_id": attempt_id,
            "tests": {"returncode": tests_result.returncode,
                      "stderr": (tests_result.stderr or "")[:400]},
        }
    if not confirm:
        return {"ok": False, "reason": "not confirmed", "attempt_id": attempt_id}

    prev_patch = hack_path.read_text(encoding="utf-8") if hack_path.exists() else None
    status = "applied"
    update_attempt(attempt_id, status="running")
    try:
        applied, _, errors = patches.apply_hacks(target=_target_rel(patch_id),
                                                 write=False)
        if errors:
            raise RuntimeError("; ".join(errors))
        if patch_id not in applied:
            raise RuntimeError(
                f"patch {patch_id!r} not in pristine reset — rewritten patch may "
                f"not match upstream; refusing to proceed")
        _write_patch(hack_path, rewritten)
        tests_result = _run_tests(repo, timeout=timeout)
        doc = doctor_checks(relevant_doctor_checks())
        if not tests_pass(tests_result):
            raise RuntimeError(
                f"tests failed after rewrite (rc={tests_result.returncode})")
        bad = [c for c in doc if not c.get("ok")]
        if bad:
            raise RuntimeError("doctor failed: " +
                               "; ".join(f"{c['name']}: {c['msg']}" for c in bad))
    except Exception as e:
        try:
            if prev_patch is not None:
                _write_patch(hack_path, prev_patch)
            elif hack_path.exists():
                hack_path.unlink()
            patches.apply_hacks()
        except Exception as e2:
            update_attempt(attempt_id, status="rolled_back", error=str(e),
                           notes=f"rollback also failed: {e2}")
            return {"ok": False, "status": "rolled_back", "error": str(e),
                    "rollback_error": str(e2), "attempt_id": attempt_id}
        update_attempt(attempt_id, status="rolled_back", error=str(e))
        return {"ok": False, "status": "rolled_back", "error": str(e),
                "attempt_id": attempt_id, "rolled_back": True}

    update_attempt(attempt_id, status=status, error=None)
    return {"ok": True, "status": status, "attempt_id": attempt_id,
            "tests": True, "doctor": True}


# ── human report ─────────────────────────────────────────────────────────────
def human_report(attempt) -> str:
    """Human-facing report: what failed, why, what was tried, manual steps."""
    pid = str(attempt.get("patch_id", ""))
    lines = [
        f"AI update engine — patch {pid!r} (attempt {attempt.get('id', '?')})",
        f"  status : {attempt.get('status', '?')}"
        f"  versions: {attempt.get('current_version', '?')} → "
        f"{attempt.get('upstream_version', '?')}",
        f"  mode   : {attempt.get('mode', '?')}"
        f"  engine : {attempt.get('engine', '?')}",
    ]
    if attempt.get("error"):
        lines.append(f"  error  : {attempt.get('error')}")
    if attempt.get("notes"):
        lines.append(f"  notes  : {attempt.get('notes')}")
    lines.append("")
    lines.append("What failed:")
    lines.append(f"  {pid} could not apply against the new upstream source.")
    lines.append("What was tried:")
    lines.append("  - deterministic diagnosis (api_renamed / path_moved / "
                 "file_deleted / format_changed / conflict / unknown)")
    lines.append("  - rewrite of the patch (fallback engine or configured LLM)")
    lines.append("  - tests + doctor verification")
    lines.append("Manual steps:")
    lines.append(f"  1. Inspect {pid} in hacks/ against the new upstream file.")
    lines.append("  2. Re-anchor the patch to the renamed/moved symbols.")
    lines.append("  3. Run: atropos doctor && python3 -m unittest discover tests")
    lines.append("  4. Re-run the update pipeline when verified.")
    return "\n".join(lines)
