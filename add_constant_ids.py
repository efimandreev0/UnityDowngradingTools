#!/usr/bin/env python3
"""Add AC ConstantID components to scenes for objects that lack one.

AC did not always assign a ConstantID; some objects were resolved through direct
references, which do not survive in Unity. For each GameObject that an Action
references but that has no ConstantID component, this adds one, registers it in
the GameObject's m_Component list, and patches the matching constantID field in
the asset.

Idempotent: an existing constantID already written into an asset is reused.
Each scene gets a .unity.bak3 backup.
"""

import argparse
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

CONSTANT_ID_GUID = b"e69a253b7feb0ee07067e60cc56fa2c7"

HEADER_RE = re.compile(rb"^--- !u!(\d+) &(-?\d+)", re.MULTILINE)
M_GAMEOBJECT_RE = re.compile(rb"^  m_GameObject:\s*\{fileID:\s*(-?\d+)\}", re.MULTILINE)
CONSTANT_ID_VALUE_RE = re.compile(rb"^  constantID:\s*(-?\d+)", re.MULTILINE)
SCRIPT_GUID_RE = re.compile(
    rb"^  m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-fA-F]+),", re.MULTILINE)
REF_LINE_RE = re.compile(
    rb"^(\s*)(\w+):\s*\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]{32}),\s*type:\s*\d+\}\s*$",
    re.MULTILINE)
ANY_CONST_LINE_RE = re.compile(
    rb"^(\s*)(\w*[Cc]onstant[Ii][Dd])(:\s*)(-?\d+)(\s*)$", re.MULTILINE)

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

# Suffixes stripped from a ref field name to match its constantID field by prefix.
FIELD_SUFFIXES = (b"object", b"target", b"list", b"path", b"item", b"clip",
                  b"sound", b"hotspot", b"asset", b"file", b"data", b"param",
                  b"trigger")


def read_scene_guid(meta_path: Path) -> Optional[bytes]:
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"^guid:\s*([0-9a-fA-F]{32})", text, re.MULTILINE)
    return m.group(1).lower().encode("ascii") if m else None


def parse_blocks(data: bytes) -> Tuple[List[Tuple[int, int, int, int]], Dict[int, int]]:
    """Return (blocks, component_to_go).

    blocks: list of (start, end, class_id, file_id).
    component_to_go: any component/GO fileID -> its GameObject fileID.
    """
    hdrs = [(m.start(), int(m.group(1)), int(m.group(2))) for m in HEADER_RE.finditer(data)]
    hdrs.append((len(data), None, None))
    blocks = []
    comp_to_go: Dict[int, int] = {}
    for i in range(len(hdrs) - 1):
        s, cls, fid = hdrs[i]
        e = hdrs[i + 1][0]
        blocks.append((s, e, cls, fid))
        if cls == 1:
            comp_to_go[fid] = fid
        else:
            mg = M_GAMEOBJECT_RE.search(data[s:e])
            if mg:
                comp_to_go[fid] = int(mg.group(1))
    return blocks, comp_to_go


def find_existing_constant_ids(blocks, data: bytes) -> Tuple[Set[int], Set[int]]:
    """Return (used constantID numbers, GO fileIDs that already have ConstantID)."""
    used: Set[int] = set()
    gos: Set[int] = set()
    for s, e, cls, fid in blocks:
        if cls != 114:
            continue
        body = data[s:e]
        ms = SCRIPT_GUID_RE.search(body)
        if not ms or ms.group(1).lower() != CONSTANT_ID_GUID:
            continue
        mc = CONSTANT_ID_VALUE_RE.search(body)
        if mc:
            used.add(int(mc.group(1)))
        mg = M_GAMEOBJECT_RE.search(body)
        if mg:
            gos.add(int(mg.group(1)))
    return used, gos


def field_base(field: bytes) -> bytes:
    base = field.lower()
    for suf in FIELD_SUFFIXES:
        if base.endswith(suf):
            return base[:-len(suf)]
    return base


def paired_constant_value(block: bytes, field: bytes) -> Optional[int]:
    """Current non-zero value of the constantID field paired with `field`."""
    fp = re.compile(rb"^\s*" + re.escape(field) + rb":\s*\{fileID:\s*-?\d+(?:,[^}]*)?\}\s*$",
                    re.MULTILINE)
    fm = fp.search(block)
    if not fm:
        return None
    cands = list(ANY_CONST_LINE_RE.finditer(block))
    if not cands:
        return None
    best = _pick_constant_line(cands, field, fm.start())
    if best is None:
        return None
    val = int(best.group(4))
    return val if val != 0 else None


def _pick_constant_line(cands, field: bytes, ref_pos: int):
    base = field_base(field)
    for cm in cands:
        if base and len(base) > 1 and cm.group(2).lower().startswith(base):
            return cm
    cands = sorted(cands, key=lambda cm: abs(cm.start() - ref_pos))
    if not cands or abs(cands[0].start() - ref_pos) > 800:
        return None
    return cands[0]


def collect_scene_refs(backup_assets: Path, scene_guid: bytes,
                       log: logging.Logger) -> Dict[Path, Dict[int, Dict[bytes, int]]]:
    """For one scene: {asset_path: {block_id: {field: target_fileID}}}."""
    result: Dict[Path, Dict[int, Dict[bytes, int]]] = {}
    t0 = time.time()
    for ext in TARGET_EXTS:
        for path in backup_assets.rglob(f"*{ext}"):
            try:
                data = path.read_bytes()
            except Exception:
                continue
            if not data.startswith(b"%YAML") or scene_guid not in data:
                continue
            blocks, _ = parse_blocks(data)
            for s, e, cls, fid in blocks:
                for m in REF_LINE_RE.finditer(data[s:e]):
                    field = m.group(2)
                    if field in INTERNAL_FIELDS:
                        continue
                    if m.group(4).lower() != scene_guid:
                        continue
                    result.setdefault(path, {}).setdefault(fid, {})[field] = int(m.group(3))
    log.info("  backup candidates: %d (%.1fs)", len(result), time.time() - t0)
    return result


def build_constant_block(file_id: int, go_id: int, constant_id: int) -> bytes:
    return (
        f"--- !u!114 &{file_id}\n"
        f"MonoBehaviour:\n"
        f"  m_ObjectHideFlags: 0\n"
        f"  m_PrefabParentObject: {{fileID: 0}}\n"
        f"  m_PrefabInternal: {{fileID: 0}}\n"
        f"  m_GameObject: {{fileID: {go_id}}}\n"
        f"  m_Enabled: 1\n"
        f"  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {CONSTANT_ID_GUID.decode()}, type: 3}}\n"
        f"  m_Name:\n"
        f"  m_EditorClassIdentifier:\n"
        f"  constantID: {constant_id}\n"
        f"  retainInPrefab: 0\n"
        f"  autoManual: 0\n"
    ).encode("ascii")


def unique_constant(used: Set[int]) -> int:
    while True:
        n = random.randint(100000, 9999999)
        if n not in used:
            used.add(n)
            return n


def register_component(data: bytes, go_fileid: int, comp_fileid: int) -> bytes:
    """Append a m_Component entry to GameObject `go_fileid`."""
    mh = re.search(rb"^--- !u!1 &" + str(go_fileid).encode("ascii") + rb"\s*$",
                   data, re.MULTILINE)
    if not mh:
        return data
    nxt = HEADER_RE.search(data, mh.end())
    block = data[mh.start():nxt.start() if nxt else len(data)]
    mc = re.search(rb"^  m_Component:\s*\n((?:  - component:\s*\{fileID:\s*-?\d+\}\s*\n)+)",
                   block, re.MULTILINE)
    if not mc:
        return data
    line = f"  - component: {{fileID: {comp_fileid}}}\n".encode("ascii")
    at = mh.start() + mc.end(1)
    return data[:at] + line + data[at:]


def process_scene(scene_path: Path, scene_guid: bytes, backup_assets: Path,
                  current_assets: Path, dry_run: bool, log: logging.Logger) -> None:
    log.info("=== %s ===", scene_path.name)
    data = scene_path.read_bytes()
    blocks, comp_to_go = parse_blocks(data)
    used_nums, gos_with_const = find_existing_constant_ids(blocks, data)
    log.info("  %d blocks, %d ConstantID components present", len(blocks), len(gos_with_const))
    max_fid = max((b[3] for b in blocks), default=100)

    refs = collect_scene_refs(backup_assets, scene_guid, log)
    targets = {tid for blk in refs.values() for fmap in blk.values()
               for tid in fmap.values()}

    # fileID >= 1000000 is a synthetic id (SceneSettings etc.), not a real GO.
    needed_gos: Set[int] = set()
    skipped = 0
    for tid in targets:
        if tid >= 1000000 or tid < 0:
            skipped += 1
            continue
        go = comp_to_go.get(tid)
        if go is None:
            skipped += 1
        elif go not in gos_with_const:
            needed_gos.add(go)
    if skipped:
        log.info("  skipped synthetic/missing targets: %d", skipped)
    log.info("  unique targets: %d, GOs without ConstantID: %d", len(targets), len(needed_gos))

    # Reuse a constantID already written into an asset so reruns stay stable.
    preferred: Dict[int, int] = {}
    for bpath, blk in refs.items():
        cur = current_assets / bpath.relative_to(backup_assets)
        if not cur.exists():
            continue
        try:
            cur_data = cur.read_bytes()
        except Exception:
            continue
        cur_block_map = {b[3]: (b[0], b[1]) for b in parse_blocks(cur_data)[0]}
        for bid, fmap in blk.items():
            if bid not in cur_block_map:
                continue
            cs, ce = cur_block_map[bid]
            cur_block = cur_data[cs:ce]
            for field, tid in fmap.items():
                if tid >= 1000000 or tid < 0:
                    continue
                go = comp_to_go.get(tid)
                if go in needed_gos:
                    existing = paired_constant_value(cur_block, field)
                    if existing:
                        preferred.setdefault(go, existing)
    if preferred:
        log.info("  GOs with a constantID already in an asset: %d", len(preferred))

    go_to_cid: Dict[int, int] = {}
    for go in needed_gos:
        if go in preferred:
            used_nums.add(preferred[go])
            go_to_cid[go] = preferred[go]
        else:
            go_to_cid[go] = unique_constant(used_nums)

    new_blocks = []
    fid = max_fid
    for go, cid in go_to_cid.items():
        fid += 1
        new_blocks.append((fid, go, cid))
    log.info("  components to add: %d", len(new_blocks))

    if not dry_run and new_blocks:
        new_data = data
        for comp_fid, go, _ in new_blocks:
            new_data = register_component(new_data, go, comp_fid)
        if not new_data.endswith(b"\n"):
            new_data += b"\n"
        for comp_fid, go, cid in new_blocks:
            new_data += build_constant_block(comp_fid, go, cid)
        bak = scene_path.with_suffix(scene_path.suffix + ".bak3")
        if not bak.exists():
            bak.write_bytes(data)
        scene_path.write_bytes(new_data)
        log.info("  scene written")

    patch_assets(refs, comp_to_go, go_to_cid, gos_with_const, blocks, data,
                 backup_assets, current_assets, dry_run, log)


def patch_assets(refs, comp_to_go, go_to_cid, gos_with_const, scene_blocks,
                 scene_data, backup_assets, current_assets, dry_run, log) -> None:
    patches = misses = 0
    for bpath, blk in refs.items():
        cur = current_assets / bpath.relative_to(backup_assets)
        if not cur.exists():
            continue
        cur_data = cur.read_bytes()
        cur_block_map = {b[3]: (b[0], b[1]) for b in parse_blocks(cur_data)[0]}
        modified = False
        for bid, fmap in blk.items():
            if bid not in cur_block_map:
                continue
            cs, ce = cur_block_map[bid]
            cur_block = cur_data[cs:ce]
            new_block = cur_block
            for field, tid in fmap.items():
                go = comp_to_go.get(tid, tid)
                cid = go_to_cid.get(go) or _existing_constant_of_go(scene_blocks, scene_data, go)
                if not cid:
                    misses += 1
                    continue
                patched = _patch_field(new_block, field, cid)
                if patched is None:
                    misses += 1
                    continue
                new_block = patched
                patches += 1
            if new_block != cur_block:
                cur_data = cur_data[:cs] + new_block + cur_data[ce:]
                modified = True
        if modified and not dry_run:
            cur.write_bytes(cur_data)
    log.info("  asset patches: %d, misses: %d", patches, misses)


def _existing_constant_of_go(blocks, data: bytes, go: int) -> Optional[int]:
    for s, e, cls, fid in blocks:
        if cls != 114:
            continue
        body = data[s:e]
        ms = SCRIPT_GUID_RE.search(body)
        if not ms or ms.group(1).lower() != CONSTANT_ID_GUID:
            continue
        mg = M_GAMEOBJECT_RE.search(body)
        if mg and int(mg.group(1)) == go:
            mc = CONSTANT_ID_VALUE_RE.search(body)
            if mc:
                return int(mc.group(1))
    return None


def _patch_field(block: bytes, field: bytes, cid: int) -> Optional[bytes]:
    fm = re.search(rb"^\s*" + re.escape(field) + rb":\s*\{fileID:\s*0\}\s*$",
                   block, re.MULTILINE)
    if not fm:
        return None
    cands = [cm for cm in ANY_CONST_LINE_RE.finditer(block) if int(cm.group(4)) == 0]
    if not cands:
        return None
    best = _pick_constant_line(cands, field, fm.start())
    if best is None:
        return None
    repl = best.group(1) + best.group(2) + best.group(3) + str(cid).encode("ascii") + best.group(5)
    return block[:best.start()] + repl + block[best.end():]


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="add missing AC ConstantID components")
    parser.add_argument("--current", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--scenes", nargs="+", required=True,
                        help="scene names, or 'all'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    current = Path(args.current).resolve()
    backup = Path(args.backup).resolve()
    cur_assets = current / "Assets"
    bak_assets = backup / "Assets"

    log_path = current / "add_constant_ids.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("add_cid")
    log.info("current: %s | backup: %s | scenes: %s", current, backup, args.scenes)

    all_scenes = list(cur_assets.rglob("*.unity"))
    if args.scenes == ["all"]:
        scenes = all_scenes
    else:
        wanted = set(args.scenes)
        scenes = [s for s in all_scenes if s.stem in wanted]
    log.info("scenes to process: %d", len(scenes))

    for scene in scenes:
        guid = read_scene_guid(scene.with_suffix(".unity.meta"))
        if not guid:
            log.warning("no guid for %s", scene)
            continue
        process_scene(scene, guid, bak_assets, cur_assets, args.dry_run, log)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
