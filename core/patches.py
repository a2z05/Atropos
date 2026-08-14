#!/usr/bin/env python3
"""Atropos patches — declarative hack engine, stdlib only.

Hacks live in hacks/*.yml (see core/config.py for the minimal YAML parser).
Each hack: id, target (path relative to hermes-agent root), old, new,
apply_after (order dependency), verify (list of greps that must be present
after apply).

atropos patch = reset target to pristine (git show HEAD) → apply hacks in
order → ast.parse (for .py) → verify greps → write.
"""
import ast
import subprocess
from pathlib import Path

from . import config, detect

HACKS_DIR = Path(__file__).resolve().parent.parent / "hacks"


def load_hacks() -> list:
    """Load all hacks/*.yml sorted by id."""
    hacks = []
    if not HACKS_DIR.exists():
        return hacks
    for f in sorted(HACKS_DIR.glob("*.yml")):
        try:
            data = config.parse_yaml(f.read_text(encoding="utf-8"))
            if "id" not in data or "old" not in data or "new" not in data:
                continue
            data["_file"] = f.name
            hacks.append(data)
        except Exception as e:
            print(f"[patches] skip {f.name}: {e}")
    # topo sort by apply_after
    ordered = []
    pending = list(hacks)
    while pending:
        for h in pending:
            dep = h.get("apply_after")
            if not dep or dep in [x["id"] for x in ordered]:
                ordered.append(h)
                pending.remove(h)
                break
        else:
            # cycle or missing dep: append remaining as-is
            ordered.extend(pending)
            break
    return ordered


def _pristine(target_rel: str) -> str:
    """Get pristine file content from hermes-agent git HEAD."""
    repo = detect.hermes_agent()
    if not repo:
        raise FileNotFoundError("hermes-agent not found")
    rel = target_rel.lstrip("/")
    out = subprocess.run(
        ["git", "-C", repo, "show", f"HEAD:{rel}"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return out.stdout


def _target_path(target_rel: str) -> Path:
    repo = detect.hermes_agent()
    if not repo:
        raise FileNotFoundError("hermes-agent not found")
    return Path(repo) / target_rel.lstrip("/")


def _guest_gated_ids():
    """Hack ids that are skipped when guest mode is disabled."""
    from . import guest
    if guest.is_enabled():
        return set()
    return set(guest.GUEST_HACK_IDS)


def apply_hacks(hacks=None, target=None, write=True, force_guest=False):
    """Apply hacks to a target file (default adapter.py). Returns (applied, skipped, errors).

    When ``force_guest`` is True (used by ``guest --toggle``), guest gating is
    bypassed so the caller controls exactly which hacks land.
    """
    hacks = hacks or load_hacks()
    gated = set() if force_guest else _guest_gated_ids()
    # group by target file
    by_target = {}
    for h in hacks:
        t = h.get("target", "plugins/platforms/telegram/adapter.py")
        by_target.setdefault(t, []).append(h)

    applied, skipped, errors = [], [], []
    for t, hs in by_target.items():
        if target and t != target:
            continue
        try:
            src = _pristine(t)
        except Exception as e:
            errors.append(f"{t}: pristine fetch failed: {e}")
            continue
        for h in hs:
            old, new = h.get("old", ""), h.get("new", "")
            if h["id"] in gated:
                skipped.append((h["id"], "guest mode disabled"))
                continue
            if not old:
                skipped.append((h["id"], "no old anchor"))
                continue
            if old not in src:
                skipped.append((h["id"], "anchor not found"))
                continue
            if src.count(old) > 1:
                skipped.append((h["id"], f"anchor not unique ({src.count(old)}x)"))
                continue
            src = src.replace(old, new, 1)
            applied.append(h["id"])
        # verify greps
        ok = True
        for h in hs:
            for g in h.get("verify", []):
                if g not in src:
                    errors.append(f"{h['id']}: verify grep missing: {g}")
                    ok = False
        # ast check for python targets
        if t.endswith(".py") and ok:
            try:
                ast.parse(src)
            except SyntaxError as e:
                errors.append(f"{t}: AST parse failed after patches: {e}")
                ok = False
        if ok and write:
            _target_path(t).parent.mkdir(parents=True, exist_ok=True)
            _target_path(t).write_text(src, encoding="utf-8")
    return applied, skipped, errors


def verify():
    """Check which hacks are currently applied to the live files (no write)."""
    hacks = load_hacks()
    results = []
    for h in hacks:
        t = h.get("target", "plugins/platforms/telegram/adapter.py")
        try:
            content = _target_path(t).read_text(errors="ignore")
            marker = h.get("verify", [h.get("new", "")[:40]])
            ok = all(g in content for g in marker if g)
            results.append({"id": h["id"], "applied": ok, "target": t})
        except Exception as e:
            results.append({"id": h["id"], "applied": False, "error": str(e)})
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.verify:
        for r in verify():
            print(f"  {'✅' if r['applied'] else '❌'} {r['id']} -> {r.get('target','')}")
    else:
        applied, skipped, errors = apply_hacks()
        print(f"applied: {len(applied)}: {applied}")
        if skipped:
            print(f"skipped: {skipped}")
        if errors:
            print(f"errors: {errors}")
