"""Dev helper: parse every hack file and report block-scalar health."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import config

hacks_dir = Path(__file__).resolve().parent.parent / "hacks"
errors = 0
for f in sorted(hacks_dir.glob("*.yml")):
    if f.name == ".gitkeep":
        continue
    try:
        data = config.parse_yaml(f.read_text(encoding="utf-8"))
        old = data.get("old", "")
        new = data.get("new", "")
        ok_old = bool(old and old.strip())
        ok_new = bool(new and new.strip())
        ok = ok_old and ok_new
        if not ok:
            errors += 1
        status = "OK" if ok else "!! EMPTY BLOCK !!"
        print(f"{f.name:40s} id={data.get('id','?'):45s} "
              f"old={len(old):5d} new={len(new):5d} {status}")
        if f.name in ("01-import-typehandler.yml",
                      "02-effective-update-guest-hide.yml",
                      "07-reaction-bridge.yml"):
            print("   OLD repr:", repr(old))
            print("   NEW head:", repr(new[:120]))
    except Exception as e:
        errors += 1
        print(f"{f.name:40s} ERROR: {e}")
print()
print("TOTAL ERRORS:", errors)
