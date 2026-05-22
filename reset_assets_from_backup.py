#!/usr/bin/env python3
"""Rebuild .asset files from the backup with the base fixes reapplied.

Use this to undo a bad restore pass: it takes each asset from the backup,
reapplies the format fixes and scene-ref stripping, and writes it to the current
project. The result is the state right before restore_constant_ids.py.
.unity scenes are not touched.
"""

import re
import sys
import time
from pathlib import Path

OLD_CORR = b"m_CorrespondingSourceObject:"
NEW_CORR = b"m_PrefabParentObject:       "  # same length
PREFAB_ASSET_RE = re.compile(rb"^( +)m_PrefabAsset: \{fileID: 0\}\n", re.MULTILINE)
PREFAB_INSTANCE_RE = re.compile(rb"^( +)m_PrefabInstance:( .*)$", re.MULTILINE)

TARGET_EXTS = (".asset", ".controller", ".anim", ".mat", ".prefab", ".guiskin")


def collect_scene_guids(assets: Path) -> set:
    out = set()
    for meta in assets.rglob("*.unity.meta"):
        try:
            text = meta.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r"^guid:\s*([0-9a-fA-F]{32})", text, re.MULTILINE)
        if m:
            out.add(m.group(1).lower().encode("ascii"))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: reset_assets_from_backup.py <current> <backup>")
        return 2
    cur_assets = Path(sys.argv[1]).resolve() / "Assets"
    bak_assets = Path(sys.argv[2]).resolve() / "Assets"

    print("collecting scene guids...")
    scene_guids = collect_scene_guids(cur_assets)
    print(f"  scenes: {len(scene_guids)}")

    alt = b"|".join(re.escape(g) for g in scene_guids)
    scene_ref_re = re.compile(
        rb"\{fileID:\s*-?\d+,\s*guid:\s*(?:" + alt + rb"),\s*type:\s*\d+\}",
        re.IGNORECASE)

    t0 = time.time()
    last = t0
    seen = written = 0
    for ext in TARGET_EXTS:
        for bpath in bak_assets.rglob(f"*{ext}"):
            seen += 1
            cur = cur_assets / bpath.relative_to(bak_assets)
            if not cur.exists():
                continue
            try:
                data = bpath.read_bytes()
            except Exception:
                continue
            if not data.startswith(b"%YAML"):
                continue
            data = data.replace(OLD_CORR, NEW_CORR)
            data = PREFAB_ASSET_RE.sub(b"", data)
            data = PREFAB_INSTANCE_RE.sub(rb"\g<1>m_PrefabInternal:\g<2>", data)
            data = scene_ref_re.sub(b"{fileID: 0}", data)
            try:
                cur.write_bytes(data)
                written += 1
            except Exception as exc:
                print(f"write failed {cur}: {exc}", file=sys.stderr)
            if time.time() - last > 2:
                print(f"  {ext}: seen {seen}, written {written}", flush=True)
                last = time.time()

    print(f"seen {seen}, written {written}, {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
