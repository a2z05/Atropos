#!/usr/bin/env python3
"""Atropos doctor — health checks + auto-fix, stdlib only."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _checks():
    """Yield (name, check_fn, fix_fn) tuples. check_fn returns (ok, message)."""
    from . import detect

    # 1. Python version
    def py_check():
        v = tuple(int(x) for x in sys.version.split(".")[:2])
        return (v >= (3, 10), f"{sys.version_info.major}.{sys.version_info.minor}")

    yield ("python >= 3.10", py_check, None)

    # 2. hermes-agent exists
    def hermes_check():
        p = detect.hermes_agent()
        return (bool(p), p or "not found")

    def hermes_fix():
        # Try cloning from upstream
        d = Path.home() / "hermes-agent"
        if d.exists():
            return True, f"exists at {d}"
        try:
            subprocess.run(
                ["git", "clone", "--depth", "5",
                 "https://github.com/NousResearch/hermes-agent.git", str(d)],
                timeout=120, check=True,
            )
            return True, f"cloned to {d}"
        except Exception as e:
            return False, f"clone failed: {e}"

    yield ("hermes-agent", hermes_check, hermes_fix)

    # 3. PTB >= 22.8
    def ptb_check():
        v = detect._ptb_version()
        if not v:
            return (False, "not installed")
        try:
            major, minor = v.split(".")[:2]
            return (int(major) > 22 or (int(major) == 22 and int(minor) >= 8), v)
        except Exception:
            return (False, v)

    def ptb_fix():
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q",
             "--force-reinstall", "--no-deps", "python-telegram-bot>=22.8"],
            timeout=300, check=True,
        )
        v = detect._ptb_version()
        return (True, v)

    yield ("PTB >= 22.8", ptb_check, ptb_fix)

    # 4. Claude Code
    def claude_check():
        c = detect._find_claude()
        return (bool(c), c or "not found")

    yield ("claude code", claude_check, None)

    # 5. Patches applied
    def patch_check():
        ad = detect.hermes_agent()
        if not ad:
            return (False, "hermes-agent not found")
        target = Path(ad) / "plugins" / "platforms" / "telegram" / "adapter.py"
        if not target.exists():
            return (False, "adapter.py not found")
        content = target.read_text(errors="ignore")
        p10 = "# ATRA P10" in content
        p9 = "# ATRA: DM session-ownership guard" in content or "guest_notify" in content
        return (p10 and p9, "all patches" if (p10 and p9) else "P10" if p10 else "P9" if p9 else "none")

    yield ("patches", patch_check, None)

    # 6. Disk space
    def disk_check():
        home = str(detect.hermes_home())
        try:
            usage = shutil.disk_usage(home)
        except OSError:
            return (True, "home dir missing — skipped")
        pct = (usage.used / usage.total) * 100
        return (pct < 85, f"{pct:.0f}% used")

    yield ("disk < 85%", disk_check, None)

    # 7. Timezone
    def tz_check():
        import datetime
        import time
        # On Windows time.timezone semantics differ; report instead of fail.
        if os.name == "nt":
            return (True, "Windows — tz check skipped")
        off = time.timezone if time.daylight == 0 else time.altzone
        # +0330 = Asia/Tehran = -12600
        return (off == -12600, f"offset={off}")

    yield ("timezone Asia/Tehran", tz_check, None)


def doctor(fix=False):
    results = []
    for name, check_fn, fix_fn in _checks():
        ok, msg = check_fn()
        fixed = False
        if not ok and fix and fix_fn:
            try:
                fix_ok, fix_msg = fix_fn()
                if fix_ok:
                    ok, msg, fixed = True, fix_msg, True
                else:
                    msg = fix_msg
            except Exception as e:
                msg = f"fix failed: {e}"
        results.append({"name": name, "ok": ok, "msg": msg, "fixed": fixed})
    return results


def status_line():
    """One-liner: doctor check without fix (ASCII-safe on Windows)."""
    for name, check_fn, _ in _checks():
        ok, msg = check_fn()
        yield f"{'[OK]' if ok else '[FAIL]'} {name}: {msg}"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()
    for r in doctor(fix=args.fix):
        icon = "✅" if r["ok"] else ("🔧" if r["fixed"] else "❌")
        extra = f" (fixed)" if r["fixed"] else ""
        print(f"  {icon} {r['name']}: {r['msg']}{extra}")
