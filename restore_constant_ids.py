#!/usr/bin/env python3
"""Restore AC constantID links cleared by strip_scene_refs.py.

strip_scene_refs.py zeroes direct references to scene objects. Adventure Creator
resolves those at runtime through a constantID field instead. This reads the
original references from the backup and writes the correct constantID into the
matching ID field of each asset.

The field pairing comes from the AC sources: AssignFile(..., idField, refField)
calls give the exact mapping (for ActionTeleport, teleporter -> markerID). The
constantID values come from the ConstantID components in the current scenes.
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HEADER_RE = re.compile(rb"^--- !u!(\d+) &(-?\d+)", re.MULTILINE)
M_GAMEOBJECT_RE = re.compile(rb"^  m_GameObject:\s*\{fileID:\s*(-?\d+)\}", re.MULTILINE)
CONSTANT_ID_VALUE_RE = re.compile(rb"^  constantID:\s*(-?\d+)", re.MULTILINE)
SCRIPT_GUID_RE = re.compile(
    rb"^  m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+),", re.MULTILINE)
REF_LINE_RE = re.compile(
    rb"^(\s*)(\w+):\s*\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]{32}),\s*type:\s*\d+\}\s*$",
    re.MULTILINE)
CS_INT_FIELD_RE = re.compile(
    r"^\s*public\s+int\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|;)", re.MULTILINE)
META_GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})", re.MULTILINE)
ASSIGN_FILE_RE = re.compile(r"AssignFile\s*\(([^)]*)\)")

CONSTANT_ID_SCRIPT_GUID = b"e69a253b7feb0ee07067e60cc56fa2c7"

INTERNAL_FIELDS = {
    b"m_GameObject", b"m_Script", b"m_PrefabParentObject", b"m_PrefabInternal",
    b"m_CorrespondingSourceObject", b"m_PrefabInstance", b"m_PrefabAsset",
    b"m_Father", b"m_Children", b"m_Component", b"m_Materials", b"m_Mesh",
    b"m_LightingDataAsset", b"m_NavMeshData", b"m_OcclusionCullingData",
    b"m_Sprite", b"m_Sprites", b"m_Texture", b"m_Cubemap",
    b"m_StaticBatchRoot", b"m_ProbeAnchor", b"m_LightProbeVolumeOverride",
    b"m_AnchorOverride", b"m_LightmapParameters",
}

TARGET_EXTS = (".asset", ".controller", ".anim", ".mat", ".prefab", ".guiskin")


def build_ac_field_mapping(scripts_dir: Path, log: logging.Logger) -> Dict[bytes, Dict[bytes, bytes]]:
    """Map {script_guid: {refField: idField}} from AssignFile() calls in AC sources."""
    mapping: Dict[bytes, Dict[bytes, bytes]] = {}
    cs_files = list(scripts_dir.rglob("*.cs"))
    log.info("scanning %d .cs files", len(cs_files))
    for cs in cs_files:
        try:
            text = cs.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        meta = cs.with_suffix(".cs.meta")
        if not meta.exists():
            continue
        try:
            gm = META_GUID_RE.search(meta.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not gm:
            continue
        guid = gm.group(1).lower().encode("ascii")

        int_fields = set(m.group(1) for m in CS_INT_FIELD_RE.finditer(text))
        m_map: Dict[bytes, bytes] = {}
        for call in ASSIGN_FILE_RE.finditer(text):
            args = [a.strip() for a in call.group(1).split(",")]
            if len(args) < 2:
                continue
            # AC convention: the last two args are (idField, refField).
            id_arg, ref_arg = args[-2], args[-1]
            if ref_arg.startswith("this."):
                ref_arg = ref_arg[5:]
            if id_arg not in int_fields:
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", ref_arg):
                continue
            m_map[ref_arg.encode("ascii")] = id_arg.encode("ascii")
        if m_map:
            mapping[guid] = m_map
    log.info("scripts with a field mapping: %d", len(mapping))
    return mapping


def parse_scene(scene_path: Path) -> Dict[int, int]:
    """Map every component/GO fileID to its GameObject's constantID, where set."""
    try:
        data = scene_path.read_bytes()
    except Exception:
        return {}
    hdrs = [(m.start(), int(m.group(1)), int(m.group(2))) for m in HEADER_RE.finditer(data)]
    hdrs.append((len(data), None, None))

    comp_to_go: Dict[int, int] = {}
    go_to_const: Dict[int, int] = {}
    for i in range(len(hdrs) - 1):
        s, cls, fid = hdrs[i]
        body = data[s:hdrs[i + 1][0]]
        if cls == 1:
            comp_to_go[fid] = fid
            continue
        mg = M_GAMEOBJECT_RE.search(body)
        if mg:
            comp_to_go[fid] = int(mg.group(1))
        if cls == 114:
            sg = SCRIPT_GUID_RE.search(body)
            if sg and sg.group(1).lower() == CONSTANT_ID_SCRIPT_GUID and mg:
                mc = CONSTANT_ID_VALUE_RE.search(body)
                if mc and int(mc.group(1)) != 0:
                    go_to_const[int(mg.group(1))] = int(mc.group(1))

    return {fid: go_to_const[go] for fid, go in comp_to_go.items() if go in go_to_const}


def collect_scenes(assets: Path, log: logging.Logger) -> Dict[bytes, Dict[int, int]]:
    out: Dict[bytes, Dict[int, int]] = {}
    t0 = time.time()
    for meta in assets.rglob("*.unity.meta"):
        try:
            gm = META_GUID_RE.search(meta.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not gm:
            continue
        scene = meta.with_suffix("")
        if scene.exists():
            out[gm.group(1).lower().encode("ascii")] = parse_scene(scene)
    log.info("scenes: %d, constantID mappings: %d (%.1fs)",
             len(out), sum(len(v) for v in out.values()), time.time() - t0)
    return out


def patch_block(cur_block: bytes, bak_block: bytes, script_guid: Optional[bytes],
                ac_map: Dict[bytes, Dict[bytes, bytes]],
                scenes: Dict[bytes, Dict[int, int]]) -> Tuple[bytes, int, int]:
    refs = []
    for m in REF_LINE_RE.finditer(bak_block):
        field = m.group(2)
        if field in INTERNAL_FIELDS:
            continue
        if m.group(4).lower() in scenes:
            refs.append((field, int(m.group(3)), m.group(4).lower()))
    if not refs:
        return cur_block, 0, 0

    field_map = ac_map.get(script_guid) if script_guid else None
    patches = misses = 0
    for field, ref_fid, ref_guid in refs:
        cid = scenes.get(ref_guid, {}).get(ref_fid)
        if not cid:
            misses += 1
            continue
        id_field = field_map.get(field) if field_map else None
        if id_field is None:
            for cand in (field + b"ID", field + b"ConstantID", b"constantID"):
                if re.search(rb"^\s*" + cand + rb":\s*-?\d+\s*$", cur_block, re.MULTILINE):
                    id_field = cand
                    break
        if id_field is None:
            misses += 1
            continue
        pat = re.compile(rb"^(\s*)" + re.escape(id_field) + rb"(:\s*)0(\s*)$", re.MULTILINE)
        m = pat.search(cur_block)
        if not m:
            misses += 1
            continue
        cur_block = (cur_block[:m.start()] + m.group(1) + id_field + m.group(2)
                     + str(cid).encode("ascii") + m.group(3) + cur_block[m.end():])
        patches += 1
    return cur_block, patches, misses


def process_file(cur_path: Path, bak_path: Path,
                 ac_map: Dict[bytes, Dict[bytes, bytes]],
                 scenes: Dict[bytes, Dict[int, int]]) -> Tuple[int, int]:
    if not cur_path.exists() or not bak_path.exists():
        return (0, 0)
    try:
        cur_data = cur_path.read_bytes()
        bak_data = bak_path.read_bytes()
    except Exception:
        return (0, 0)
    if not cur_data.startswith(b"%YAML") or not bak_data.startswith(b"%YAML"):
        return (0, 0)
    if not any(g in bak_data for g in scenes):
        return (0, 0)

    def block_map(data: bytes) -> Dict[int, Tuple[int, int]]:
        h = [(m.start(), int(m.group(2))) for m in HEADER_RE.finditer(data)]
        h.append((len(data), None))
        return {h[i][1]: (h[i][0], h[i + 1][0]) for i in range(len(h) - 1)}

    bak_blocks = block_map(bak_data)
    patches = misses = 0
    changed = False
    for bid, (bs, be) in bak_blocks.items():
        cur_blocks = block_map(cur_data)
        if bid not in cur_blocks:
            continue
        cs, ce = cur_blocks[bid]
        cur_block = cur_data[cs:ce]
        sg = SCRIPT_GUID_RE.search(cur_block)
        new_block, p, m = patch_block(cur_block, bak_data[bs:be],
                                      sg.group(1).lower() if sg else None, ac_map, scenes)
        patches += p
        misses += m
        if new_block != cur_block:
            cur_data = cur_data[:cs] + new_block + cur_data[ce:]
            changed = True
    if changed:
        try:
            cur_path.write_bytes(cur_data)
        except Exception:
            pass
    return patches, misses


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="restore AC constantID links")
    parser.add_argument("--current", required=True)
    parser.add_argument("--backup", required=True)
    args = parser.parse_args(argv)

    current = Path(args.current).resolve()
    backup = Path(args.backup).resolve()
    cur_assets = current / "Assets"
    bak_assets = backup / "Assets"

    log_path = current / "restore_constant_ids.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("restore")

    ac_map = build_ac_field_mapping(cur_assets / "Scripts", log)
    scenes = collect_scenes(cur_assets, log)

    t0 = time.time()
    last = t0
    seen = changed = patches = misses = 0
    for ext in TARGET_EXTS:
        for bpath in bak_assets.rglob(f"*{ext}"):
            seen += 1
            p, m = process_file(cur_assets / bpath.relative_to(bak_assets), bpath, ac_map, scenes)
            if p:
                changed += 1
                patches += p
            misses += m
            if time.time() - last > 2:
                log.info("  %s: seen %d, changed %d, patches %d, misses %d",
                         ext, seen, changed, patches, misses)
                last = time.time()

    log.info("files seen %d, changed %d, patches %d, misses %d (%.1fs)",
             seen, changed, patches, misses, time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
