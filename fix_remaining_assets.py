#!/usr/bin/env python3
"""Rename m_CorrespondingSourceObject in every .asset file.

fix_unity_project.py uses a regex pass that can stop short inside the large
MonoBehaviour/ folder. This does a flat byte replace over all .asset files so
nothing is missed. Writes in place, no backups.
"""

import sys
import time
from pathlib import Path

OLD = b"m_CorrespondingSourceObject:"
NEW = b"m_PrefabParentObject:       "  # same length, trailing spaces are valid YAML
assert len(OLD) == len(NEW)


def fix_file(path: Path) -> int:
    try:
        data = path.read_bytes()
    except Exception:
        return 0
    if not data.startswith(b"%YAML") or OLD not in data:
        return 0
    n = data.count(OLD)
    try:
        path.write_bytes(data.replace(OLD, NEW))
    except Exception as exc:
        print(f"write failed {path}: {exc}", file=sys.stderr)
        return 0
    return n


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: fix_remaining_assets.py <project_root>")
        return 2
    assets = Path(sys.argv[1]).resolve() / "Assets"
    if not assets.exists():
        print(f"no {assets}", file=sys.stderr)
        return 2

    t0 = time.time()
    files = fixed = total = 0
    last = t0
    for path in assets.rglob("*.asset"):
        files += 1
        n = fix_file(path)
        if n:
            fixed += 1
            total += n
        if time.time() - last > 2:
            print(f"  seen {files}, changed {fixed}", flush=True)
            last = time.time()

    print(f"seen {files}, changed {fixed}, replacements {total}, {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
