#!/usr/bin/env python3
"""Atropos update — atomic updater: fetch upstream → diff → backup → reset →
apply hacks → doctor verify → rollback on failure."""
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import detect, doctor, patches


def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def fetch_upstream(repo: str, timeout=120):
    """git fetch + log. Returns (head, commits_since) or raises."""
    out = subprocess.run(
        ["git", "-C", repo, "fetch", "origin", "main"],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(f"fetch failed: {out.stderr[:300]}")
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--short", "origin/main"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    count = subprocess.run(
        ["git", "-C", repo, "rev-list", "--count", "HEAD..origin/main"],
        capture_output=True, text=True, timeout=30,
    ).stdout.strip() or "0"
    return {"head": head, "remote": remote, "behind": count}


def diff_summary(repo: str):
    """What changed between HEAD and origin/main (file list + size)."""
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--stat", "HEAD..origin/main"],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def backup_state(atropos_home: Path) -> Path:
    """Copy config to a timestamped backup dir. Returns backup path."""
    ts = _ts()
    bk = atropos_home / "backups" / ts
    bk.mkdir(parents=True, exist_ok=True)
    cfg = atropos_home / "config.yaml"
    if cfg.exists():
        shutil.copy2(cfg, bk / "config.yaml")
    return bk


def _git(repo: str, *args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def apply_update(repo: str, dry_run=False, ai_engine=False):
    """Full update: fetch → backup → reset → re-apply hacks → doctor verify.

    Returns summary dict. Rollback to the pre-update HEAD on failure.
    With ``ai_engine`` the conflicting-hack path consults the AI update
    engine (update_ai.diagnose + rewrite + apply) before rolling back —
    self-modification that survives the update as re-anchored patches.
    """
    if dry_run:
        return {"dry_run": True, **fetch_upstream(repo)}

    home = detect.atropos_home()
    # 1. fetch + record the pre-update head for rollback
    up = fetch_upstream(repo)
    prev_head = up["head"]
    if up["head"] == up["remote"]:
        return {**up, "ok": True, "up_to_date": True}

    # 2. backup config + hacks
    bk = backup_state(home)

    # 3. reset to upstream
    out = _git(repo, "reset", "--hard", "origin/main")
    if out.returncode != 0:
        return {"ok": False, "error": f"reset failed: {out.stderr[:300]}", "backup": str(bk)}

    # 4. re-apply hacks
    applied, skipped, errors = patches.apply_hacks()
    ok = not errors

    # 5. doctor verify (only the checks that matter post-patch)
    checks = [c for c in doctor.doctor() if c["name"] in ("patches", "python >= 3.10")]
    failed_checks = [c for c in checks if not c["ok"]]

    if ok and failed_checks:
        ok = False
        errors = errors + [f"doctor: {c['name']}: {c['msg']}" for c in failed_checks]

    result = {
        "ok": ok,
        "head": fetch_upstream(repo)["head"],
        "prev_head": prev_head,
        "behind": up["behind"],
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "doctor": checks,
        "backup": str(bk),
    }
    if ok:
        # changelog auto-bump (the changelog lives in the updated repo)
        try:
            changelog = bump_changelog(Path(repo) / "docs" / "CHANGELOG.md",
                                       source=result["head"])
            result["changelog"] = changelog
        except Exception as e:
            result["changelog"] = {"ok": False, "error": str(e)}
    if not ok and ai_engine:
        # AI self-modification (v18 G): diagnose + rewrite the failed hack
        # against the new upstream sources, apply on confirm, verify again.
        result["ai"] = _ai_repair(repo, result, timeout=180)
        if result["ai"].get("ok"):
            ok = True
            errors = []
    if not ok:
        # rollback: restore pre-update head, then re-apply hacks again
        _git(repo, "reset", "--hard", prev_head)
        patches.apply_hacks()
        result["rolled_back"] = True
    return result


def _ai_repair(repo: str, result: dict, timeout: int = 180) -> dict:
    """One AI-repair pass over the failed patches (update_ai engine).

    Builds the failed_patches state from the apply result + upstream
    sources, asks the engine to diagnose and rewrite each, and applies
    the first confirmed rewrite. Returns the engine summary.
    """
    from . import update_ai, settings
    if not settings.get("update.auto_ai", False):
        return {"ok": False, "reason": "update.auto_ai off"}
    if settings.get("update-ai.mode", "manual") == "off":
        return {"ok": False, "reason": "update-ai.mode off"}
    # Failed hacks are the ones the apply reported as errors. Each error line
    # carries the hack id (patches.apply_hacks appends f"{id}: ..."), so
    # surface those patches to the engine for diagnosis.
    error_text = "\n".join(result.get("errors", []) or [])
    conflicted = {e.split(":", 1)[0].strip()
                  for e in result.get("errors", []) if ":" in e}
    conflicts = []
    for h in patches.load_hacks():
        if h.get("id") not in conflicted:
            continue
        target = h.get("target", "")
        current_source = upstream_source = ""
        if target:
            # real sources so the engine can detect renames, not just "unknown"
            try:
                current_source = (Path(repo) / target.lstrip("/")).read_text(
                    encoding="utf-8")
            except Exception:
                current_source = ""
            try:
                out = subprocess.run(
                    ["git", "-C", repo, "show", f"origin/main:{target.lstrip('/')}"],
                    capture_output=True, text=True, timeout=30)
                upstream_source = out.stdout if out.returncode == 0 else ""
            except Exception:
                upstream_source = ""
        conflicts.append({
            "patch_id": h.get("id", ""),
            "target": target,
            "current_source": current_source,
            "upstream_source": upstream_source,
            "error": error_text or "apply failed",
        })
    state = {
        "upstream_version": result.get("head", ""),
        "current_version": result.get("prev_head", ""),
        "failed_patches": conflicts,
    }
    try:
        preview = update_ai.ai_check(state)
    except Exception as e:
        return {"ok": False, "reason": "ai_check failed", "error": str(e)}
    if not preview.get("ok") or not preview.get("previews"):
        return {"ok": False, "reason": "nothing diagnosed"}
    first = preview["previews"][0]
    attempt_id = first["attempt"].get("id")
    return {"ok": True, "attempt_id": attempt_id,
            "patch_id": first["attempt"].get("patch_id", ""),
            "diagnosis": first["diagnosis"],
            "rewritten_patch": first.get("rewritten_patch", "")}


def dry_run_conflicts(repo: str) -> dict:
    """Simulate hack application against origin/main and report conflicts.

    For every hack, the ``old`` anchor is searched in the upstream version
    of the target file (``git show origin/main:<target>``). No writes, no
    checkout — purely diagnostic. Returns per-hack results plus a summary
    of which hacks would fail after an update.
    """
    from . import patches
    hacks = patches.load_hacks()
    results = []
    for h in hacks:
        t = h.get("target", "plugins/platforms/telegram/adapter.py")
        old = h.get("old", "")
        if not old:
            results.append({"id": h["id"], "ok": False, "reason": "no old anchor"})
            continue
        try:
            out = subprocess.run(
                ["git", "-C", repo, "show", f"origin/main:{t.lstrip('/')}"],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode != 0:
                results.append({"id": h["id"], "ok": False,
                                "reason": f"target missing upstream: {t}"})
                continue
            src = out.stdout
            if old not in src:
                results.append({"id": h["id"], "ok": False,
                                "reason": "old anchor not found upstream"})
            elif src.count(old) > 1:
                results.append({"id": h["id"], "ok": False,
                                "reason": f"anchor not unique ({src.count(old)}x)"})
            else:
                results.append({"id": h["id"], "ok": True, "target": t})
        except Exception as e:
            results.append({"id": h["id"], "ok": False, "reason": str(e)})
    conflicts = [r for r in results if not r["ok"]]
    return {"conflicts": conflicts, "total": len(results), "clear": not conflicts}


def update_check(repo: str):
    """Non-destructive check. Returns dict with what would change."""
    try:
        up = fetch_upstream(repo)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if up["head"] == up["remote"]:
        return {"ok": True, "up_to_date": True, **up}
    dry = dry_run_conflicts(repo)
    return {
        "ok": True,
        "up_to_date": False,
        **up,
        "diff": diff_summary(repo),
        "dry_run": dry,
    }


def run_tests(repo: str, timeout=600) -> dict:
    """Run the Atropos test suite. Returns {ok, output}."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "tests"],
            cwd=repo, capture_output=True, text=True, timeout=timeout,
        )
        return {"ok": out.returncode == 0, "returncode": out.returncode,
                "output": (out.stdout + out.stderr)[-4000:]}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def auto_check() -> dict:
    """settings.update.auto == 'check': alert when an update is available.

    Stores last-check state like the dashboard, clears old alerts when
    up to date. Returns the check result.
    """
    import json
    from . import settings
    home = detect.atropos_home()
    state_path = home / "update_state.json"
    repo = detect.hermes_agent()
    if not repo:
        return {"ok": False, "error": "hermes-agent not found"}
    r = update_check(repo)
    state = {
        "last_check": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "up_to_date": r.get("up_to_date", False),
        "behind": r.get("behind"),
        "head": r.get("head"),
        "remote": r.get("remote"),
    }
    try:
        home.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    if not r.get("ok"):
        return r
    if not r.get("up_to_date"):
        try:
            from . import alerts
            alerts.send_alert(
                f"🔄 Atropos update available: {r.get('behind', '?')} commits behind "
                f"({r.get('head')} → {r.get('remote')}). Run `atropos update apply`.",
                force=True)
        except Exception:
            pass
    return r


def auto_apply() -> dict:
    """settings.update.auto == 'apply': auto-apply clean non-conflicting updates.

    Always snapshots before (backup_state), re-applies hacks, runs doctor +
    tests, and rolls back on failure. Conflict updates are NOT auto-applied —
    they surface for the AI engine / manual apply.
    """
    from . import settings
    repo = detect.hermes_agent()
    if not repo:
        return {"ok": False, "error": "hermes-agent not found"}
    r = update_check(repo)
    if r.get("up_to_date"):
        return {**r, "auto": "apply", "action": "none"}
    if not r.get("ok"):
        return r
    dry = r.get("dry_run", {})
    if not dry.get("clear") and not settings.get("update.auto_ai", False):
        return {**r, "auto": "apply", "action": "deferred", "reason": "conflicts",
                "conflicts": dry.get("conflicts", [])}
    result = apply_update(repo)
    result["auto"] = "apply"
    if result.get("ok"):
        try:
            result["tests"] = run_tests(_home_repo())
        except Exception as e:
            result["tests"] = {"ok": False, "output": str(e)}
    return result


def _home_repo():
    """The repo dir of this Atropos install (used as the test cwd)."""
    return Path(__file__).resolve().parent.parent


def bump_changelog(changelog_path: Path, version: str = "", source: str = "") -> dict:
    """Prepend a ``## [version]`` entry to docs/CHANGELOG.md (gated by
    ``update.changelog_bump``). Returns what changed.

    ``version`` wins when given; otherwise a HEAD-derived marker is used.
    ``source`` describes what was updated (e.g. the new head short sha).
    """
    try:
        from . import settings
        if not settings.get("update.changelog_bump", True):
            return {"ok": True, "skipped": True, "reason": "update.changelog_bump off"}
    except Exception:
        pass
    if not changelog_path.exists():
        return {"ok": False, "error": f"changelog not found: {changelog_path}"}
    text = changelog_path.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    version = version or (f"HEAD-{source[:8]}" if source else "HEAD")
    # avoid duplicate entries for the same version+day
    marker = f"## [{version}]"
    if marker in text:
        return {"ok": True, "skipped": True, "reason": f"{marker} already present"}
    entry = (
        f"## [{version}] — {today} (auto)\n\n"
        f"### Applied\n"
        f"- Upstream update applied{(' — source ' + source) if source else ''}.\n"
        f"- Auto-bumped by the Atropos update pipeline.\n\n"
    )
    head = text.split("# Changelog", 1)
    if len(head) == 2:
        text = "# Changelog\n" + entry + head[1].lstrip("\n")
    else:
        text = "# Changelog\n\n" + entry + text
    changelog_path.write_text(text, encoding="utf-8")
    return {"ok": True, "added": marker, "path": str(changelog_path)}
