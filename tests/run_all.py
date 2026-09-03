"""Run every test module. Exit non-zero if any fails."""
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
mods = sorted(p for p in here.glob("test_*.py"))
bad = []
for m in mods:
    print(f"\n----- {m.name} " + "-" * (60 - len(m.name)))
    r = subprocess.run([sys.executable, str(m)], cwd=here.parent)
    if r.returncode != 0:
        bad.append(m.name)
print()
if bad:
    print(f"FAILED: {', '.join(bad)}")
    sys.exit(1)
print(f"all {len(mods)} test modules passed")
