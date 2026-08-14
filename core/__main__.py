#!/usr/bin/env python3
"""Atropos — ``python3 -m core`` entrypoint.

Adds the repo root to sys.path and executes the ``atropos`` CLI via exec()
to avoid circular-import issues with ``runpy.run_path``.
"""
import os
import sys

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

_SCRIPT = os.path.join(_REPO_DIR, "atropos")

if __name__ == "__main__":
    sys.argv[0] = _SCRIPT
    with open(_SCRIPT, "r", encoding="utf-8") as _f:
        _code = _f.read()
    # Execute in a fresh namespace — sys.path is already set.
    # The ``from core import ...`` line resolves to the already-loaded
    # ``core`` package (core/__init__.py + sub-modules) so there is no
    # double-import or circular-import issue.
    exec(compile(_code, _SCRIPT, "exec"), {"__name__": "__main__", "__file__": _SCRIPT})