#!/usr/bin/env python3
"""Atropos environment detection — stdlib only, OS-agnostic.

Writes ~/.atropos/runtime.json on init/doctor. Everything else in Atropos
reads paths from this module so nothing is hardcoded.
"""
import json
import os
import platform
import shutil
import sys
from pathlib import Path


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def hermes_home() -> Path:
    """Resolve Hermes home dir: $HERMES_HOME, else ~/.hermes (or %USERPROFILE%\\.hermes on Windows)."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return _home() / ".hermes"


def atropos_home() -> Path:
    """~/.atropos (or %USERPROFILE%\\.atropos), or $ATROPOS_HOME if set."""
    env = os.environ.get("ATROPOS_HOME")
    if env:
        return Path(env)
    return _home() / ".atropos"


def _which(cmd: str) -> str:
    return shutil.which(cmd) or ""


def _find_hermes_agent() -> str:
    """Locate the hermes-agent install: $HERMES_AGENT, common dirs, or near this repo."""
    env = os.environ.get("HERMES_AGENT")
    if env and Path(env).exists():
        return env
    candidates = [
        "/opt/hermes-agent",
        "/app/hermes-agent",
        str(_home() / "hermes-agent"),
        str(_home() / "hermes"),
    ]
    if os.name == "nt":
        candidates = [
            str(_home() / "hermes-agent"),
            r"C:\hermes-agent",
            r"C:\opt\hermes-agent",
        ]
    # repo-relative: ../../ (when run from <atropos>/core/)
    repo_up = Path(__file__).resolve().parent.parent.parent / "hermes-agent"
    if repo_up.exists():
        return str(repo_up)
    for c in candidates:
        if Path(c).exists():
            return c
    return ""


def hermes_agent() -> str:
    """Public accessor for the detected hermes-agent install dir."""
    return _find_hermes_agent()


def _find_claude() -> str:
    exe = "claude.exe" if os.name == "nt" else "claude"
    w = _which(exe)
    if w:
        return w
    for c in [str(_home() / ".local/bin/claude"), str(_home() / "bin/claude")]:
        if Path(c).exists():
            return c
    return ""


def _ptb_version() -> str:
    try:
        import subprocess
        out = subprocess.run(
            [sys.executable, "-c", "import telegram; print(telegram.__version__)"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or ""
    except Exception:
        return ""


def detect_cloud() -> str:
    """Heuristic cloud detection from env vars."""
    env = os.environ
    if env.get("RAILWAY_PROJECT_ID") or env.get("RAILWAY_ENVIRONMENT"):
        return "railway"
    if env.get("K_SERVICE") or env.get("CLOUD_RUN_JOB"):
        return "cloudrun"
    if env.get("FLY_APP_NAME"):
        return "fly"
    if env.get("GOOGLE_CLOUD_PROJECT") or env.get("GCP_PROJECT"):
        return "gcp"
    if env.get("DIGITALOCEAN_APP_NAME"):
        return "digitalocean"
    if env.get("AWS_EXECUTION_ENV") or env.get("AWS_LAMBDA_FUNCTION_NAME"):
        return "aws"
    return "none"


def _package_manager() -> str:
    if shutil.which("uv"):
        return "uv"
    if shutil.which("conda") or shutil.which("mamba"):
        return "conda"
    if shutil.which("pip") or shutil.which("pip3"):
        return "pip"
    return ""


def _node_version() -> str:
    import subprocess
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def detect() -> dict:
    """Run full detection and return the runtime dict."""
    return {
        "os": platform.system().lower(),          # linux | windows | darwin
        "os_release": platform.release(),
        "python": sys.executable,
        "python_version": platform.python_version(),
        "node": _node_version(),
        "npm": _which("npm"),
        "git": _which("git"),
        "claude": _find_claude(),
        "hermes_home": str(hermes_home()),
        "hermes_agent": _find_hermes_agent(),
        "ptb_version": _ptb_version(),
        "cloud": detect_cloud(),
        "package_manager": _package_manager(),
        "os_home": str(_home()),
        "cwd": str(Path.cwd()),
    }


def _ts():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def save_runtime() -> dict:
    data = detect()
    home = atropos_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "runtime.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def is_first_run() -> bool:
    """True when the setup wizard has never completed (no .setup_done marker).

    The wizard writes ~/.atropos/.setup_done after a successful run.
    On Railway, first-run is detected by the absence of config.yaml instead
    (headless environments skip the interactive wizard).
    """
    home = atropos_home()
    return not (home / ".setup_done").exists()


def mark_setup_done():
    """Write the .setup_done sentinel so the wizard won't re-trigger."""
    home = atropos_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / ".setup_done").write_text("1", encoding="utf-8")


if __name__ == "__main__":
    d = save_runtime()
    for k, v in d.items():
        print(f"{k}: {v}")
