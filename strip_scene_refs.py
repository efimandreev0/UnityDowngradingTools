#!/usr/bin/env python3
"""Clear cross-scene direct references in non-scene asset files.

AssetRipper writes ScriptableObjects (AC Actions) with references to specific
objects inside .unity scenes. Unity cannot hold a live reference to a scene
object from an asset, so the build fails with "ReadObjectThreaded on scene
objects!". This zeroes those references; AC resolves them at runtime by
constantID instead. .unity files are left untouched.
"""

import re
import sys
import time
from pathlib import Path

TARGET_EXTS = (".asset", ".controller", ".anim", ".mat", ".prefab", ".guiskin")


def collect_scene_guids(assets: Path) -> set:
    guids = set()
    for meta in assets.rglob("*.unity.meta"):
        try:
            text = meta.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r"^guid:\s*([0-9a-fA-F]{32})", text, re.MULTILINE)
        if m:
            guids.add(m.group(1).lower().encode("ascii"))
    return guids


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: strip_scene_refs.py <project_root>")
        return 2
    project = Path(sys.argv[1]).resolve()
    assets = project / "Assets"
    if not assets.exists():
        print(f"no {assets}/Assets", file=sys.stderr)
        return 2

    print("collecting scene guids...")
    scene_guids = collect_scene_guids(assets)
    print(f"  scenes: {len(scene_guids)}")
    if not scene_guids:
        print("no scenes, nothing to do")
        return 0

    alt = b"|".join(re.escape(g) for g in scene_guids)
    pattern = re.compile(
        rb"\{fileID:\s*-?\d+,\s*guid:\s*(?:" + alt + rb"),\s*type:\s*\d+\}",
        re.IGNORECASE)

    t0 = time.time()
    last = t0
    files_seen = files_changed = refs = 0
    for ext in TARGET_EXTS:
        for path in assets.rglob(f"*{ext}"):
            files_seen += 1
            try:
                data = path.read_bytes()
            except Exception:
                continue
            if not data.startswith(b"%YAML"):
                continue
            new_data, n = pattern.subn(b"{fileID: 0}", data)
            if n:
                try:
                    path.write_bytes(new_data)
                    files_changed += 1
                    refs += n
                except Exception as exc:
                    print(f"write failed {path}: {exc}", file=sys.stderr)
            if time.time() - last > 2:
                print(f"  {ext}: seen {files_seen}, changed {files_changed}, refs {refs}",
                      flush=True)
                last = time.time()

    print(f"seen {files_seen}, changed {files_changed}, refs cleared {refs}, "
          f"{time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
