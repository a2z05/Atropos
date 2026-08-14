#!/usr/bin/env python3
"""Generate hacks/*.yml from patches/apply_guest_patch.py (authoritative).

Each hack YAML gets id, target, old, new (block scalars) and verify greps.
The old/new strings are extracted from the patch script's apply() calls via
AST, so indentation is byte-exact. The YAML emitter uses the documented
base=(opener_indent+2) block-scalar convention from core/config.py.

Run: python3 scripts/gen_hacks.py
"""
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCH_SRC = REPO / "patches" / "apply_guest_patch.py"
HACKS_DIR = REPO / "hacks"

# (hack_id, target_rel, apply_after_or_None, verify_extra) for each apply() call
# in the order they appear in apply_guest_patch.py.
PLAN = [
    # name in the script, hack id, apply_after, extra verify greps
    ("import TypeHandler", "import TypeHandler", None, ["TypeHandler,", "MessageHandler as TelegramMessageHandler,"]),
    ("effective update message extra", "effective update message extra", "import TypeHandler",
     ['getattr(update, "guest_message", None)', "return None"]),
    ("send suppress", "send suppress", "effective update message extra",
     ['if getattr(self, "_suppress_send", False):']),
    ("guest handler block", "guest handler block", "send suppress",
     ["async def _handle_guest_message(", "async def _answer_guest_text("]),
    ("register main", "register main", "guest handler block",
     ["if TELEGRAM_AVAILABLE:", "self._handle_guest_message"]),
    ("register rebuild", "register rebuild", "register main",
     ["if TELEGRAM_AVAILABLE:", "self._handle_guest_message"]),
    ("reaction bridge", "reaction bridge", "register rebuild",
     ["async def add_reaction(", "async def remove_reaction("]),
    ("processing start reaction", "processing start reaction", "reaction bridge",
     ['"\\U0001f525"']),
    ("processing done success", "processing done success", "processing start reaction",
     ['"\\u2705"']),
    ("p8 dm chat/user mismatch guard", "p8 dm chat/user mismatch guard", "processing done success",
     ["Dropped DM chat/user mismatch"]),
    ("p10 log-channel !-command intercept", "p10 log-channel !-command intercept", "p8 dm chat/user mismatch guard",
     ["# ATRA P10", 'os.environ.get("ATRA_LOG_CHANNEL"']),
    ("p9 guest notify on unauthorized", "p9 guest notify on unauthorized", "p10 log-channel !-command intercept",
     ["import guest_notify", 'reason="unauthorized"']),
]

TARGET = "plugins/platforms/telegram/adapter.py"


def esc(s):
    return repr(s)


def to_block_scalar(value: str, indent: int) -> str:
    """Serialize a multi-line / leading-space string as a block scalar.

    Uses the base=(opener_indent+2) convention: every body line is emitted
    at opener_indent+2+line_indent. The parser strips opener_indent+2 back
    off, reproducing the anchor exactly.
    """
    opener_pad = " " * indent
    body_pad = " " * (indent + 2)
    lines = value.rstrip("\n").split("\n")
    out = [f"{opener_pad}|-"]
    for ln in lines:
        if ln == "":
            out.append("")
        else:
            out.append(body_pad + ln)
    return "\n".join(out)


def main():
    src = PATCH_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = {}

    # collect all top-level/Module-level apply() / apply2() calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else None
        if name not in ("apply", "apply2"):
            continue
        if len(node.args) < 3:
            continue
        arg_name = ast.literal_eval(node.args[0])
        arg_old = ast.literal_eval(node.args[1])
        arg_new = ast.literal_eval(node.args[2])
        calls.setdefault(arg_name, (arg_old, arg_new))

    missing = []
    written = []
    for script_name, hack_id, apply_after, verify_extra in PLAN:
        if script_name not in calls:
            missing.append(script_name)
            continue
        old, new = calls[script_name]
        verify = verify_extra
        lines = []
        lines.append(f"# hack: {hack_id}")
        lines.append(f'id: "{hack_id}"')
        lines.append(f"target: {TARGET}")
        if apply_after:
            lines.append(f'apply_after: "{apply_after}"')
        lines.append("old: " + to_block_scalar(old, 0))
        lines.append("new: " + to_block_scalar(new, 0))
        lines.append("verify:")
        for g in verify:
            lines.append(f"  - {esc(g)}")
        body = "\n".join(lines) + "\n"

        num = PLAN.index((script_name, hack_id, apply_after, verify_extra)) + 1
        fname = f"{num:02d}-{hack_id.lower().replace(' ', '-').replace('!','').replace('.','')}.yml"
        HACKS_DIR.mkdir(parents=True, exist_ok=True)
        (HACKS_DIR / fname).write_text(body, encoding="utf-8")
        written.append(fname)
        print(f"WROTE {fname}  (old={len(old)} new={len(new)})")

    if missing:
        print("MISSING:", missing)
        sys.exit(1)
    print(f"\n{len(written)} hacks written.")


if __name__ == "__main__":
    main()