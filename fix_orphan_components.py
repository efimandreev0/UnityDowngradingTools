"""Register orphaned components in their GameObject's m_Component list.

add_constant_ids.py appends component blocks to a scene and adds the matching
m_Component entry. When that entry is missed, the component exists but the
GameObject does not reference it, so Unity ignores it. This scans every scene
and adds any missing m_Component entry.
"""

import re
import sys
import time
from pathlib import Path

HEADER_RE = re.compile(rb"^--- !u!(\d+) &(-?\d+)", re.MULTILINE)
M_GAMEOBJECT_RE = re.compile(rb"^  m_GameObject:\s*\{fileID:\s*(-?\d+)\}", re.MULTILINE)


def fix_scene(path: Path) -> int:
    try:
        data = path.read_bytes()
    except Exception:
        return 0
    if not data.startswith(b"%YAML"):
        return 0

    hdrs = [(m.start(), int(m.group(1)), int(m.group(2))) for m in HEADER_RE.finditer(data)]
    hdrs.append((len(data), None, None))

    component_to_go = {}
    blocks = {}
    for i in range(len(hdrs) - 1):
        s, cls, fid = hdrs[i]
        e = hdrs[i + 1][0]
        blocks[fid] = (s, e, cls)
        if cls == 1:
            continue
        mg = M_GAMEOBJECT_RE.search(data[s:e])
        if mg:
            component_to_go[fid] = int(mg.group(1))

    go_to_comps = {}
    for comp_fid, go_fid in component_to_go.items():
        go_to_comps.setdefault(go_fid, []).append(comp_fid)

    added = 0
    go_items = sorted(
        [(fid, blocks[fid]) for fid in go_to_comps
         if blocks.get(fid, (None, None, None))[2] == 1],
        key=lambda x: -x[1][0])
    for go_fid, (s, e, _) in go_items:
        body = data[s:e]
        mc = re.search(
            rb"^  m_Component:\s*\n((?:  - component:\s*\{fileID:\s*-?\d+\}\s*\n)*)",
            body, re.MULTILINE)
        if not mc:
            continue
        existing = set(int(x) for x in re.findall(rb"fileID:\s*(-?\d+)", mc.group(1)))
        missing = set(go_to_comps[go_fid]) - existing
        if not missing:
            continue
        new_lines = b"".join(
            f"  - component: {{fileID: {fid}}}\n".encode("ascii") for fid in sorted(missing))
        at = s + mc.end(1)
        data = data[:at] + new_lines + data[at:]
        added += len(missing)

    if added:
        try:
            path.write_bytes(data)
        except Exception as exc:
            print(f"write failed {path}: {exc}", file=sys.stderr)
    return added


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: fix_orphan_components.py <project_root>")
        return 2
    scenes = Path(sys.argv[1]).resolve() / "Assets" / "Scenes"
    if not scenes.exists():
        print(f"no {scenes}", file=sys.stderr)
        return 2

    t0 = time.time()
    total = files = 0
    for scene in scenes.rglob("*.unity"):
        n = fix_scene(scene)
        if n:
            files += 1
            total += n
            print(f"  {scene.name}: +{n}")
    print(f"scenes changed: {files}, refs added: {total}, {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
