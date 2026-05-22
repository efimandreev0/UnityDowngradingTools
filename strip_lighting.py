#!/usr/bin/env python3
"""Remove baked lighting from the project.

LightingData.asset from 2018.3+ uses serializedVersion 4 and breaks the build
pipeline. A 2D game does not need baked lighting, so this clears the references
in scenes and deletes the lighting data files. Lighting becomes real-time.
"""

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import List

LIGHTING_REF = re.compile(
    rb"^(  m_LightingDataAsset:) \{fileID: \d+, guid: [0-9a-fA-F]+, type: \d+\}",
    re.MULTILINE)
NAVMESH_REF = re.compile(
    rb"^(  m_NavMeshData:) \{fileID: \d+, guid: [0-9a-fA-F]+, type: \d+\}",
    re.MULTILINE)


def patch_scene(path: Path, dry_run: bool, log: logging.Logger) -> int:
    try:
        data = path.read_bytes()
    except Exception as exc:
        log.warning("read failed %s: %s", path, exc)
        return 0
    if not data.startswith(b"%YAML"):
        return 0
    data, n1 = LIGHTING_REF.subn(rb"\g<1> {fileID: 0}", data)
    new_data, n2 = NAVMESH_REF.subn(rb"\g<1> {fileID: 0}", data)
    if n1 + n2 == 0:
        return 0
    if dry_run:
        log.info("[dry] %s: lighting=%d nav=%d", path.name, n1, n2)
        return n1 + n2
    bak = path.with_suffix(path.suffix + ".bak2")
    if not bak.exists():
        shutil.copy2(path, bak)
    path.write_bytes(new_data)
    log.info("patched %s: lighting=%d nav=%d", path.name, n1, n2)
    return n1 + n2


def delete_lighting_files(scenes_root: Path, dry_run: bool, log: logging.Logger) -> int:
    targets = []
    for name in ("LightingData.asset", "LightProbes.asset", "NavMesh.asset",
                 "ReflectionProbe-0.exr"):
        targets.extend(scenes_root.rglob(name))
    deleted = 0
    for f in targets:
        meta = f.with_suffix(f.suffix + ".meta")
        if dry_run:
            log.info("[dry] rm %s", f)
            deleted += 1
            continue
        try:
            f.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("rm failed %s: %s", f, exc)
        if meta.exists():
            try:
                meta.unlink()
            except Exception:
                pass
    return deleted


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="strip baked lighting")
    parser.add_argument("--project", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    scenes = project / "Assets" / "Scenes"
    if not scenes.exists():
        print(f"no {scenes}", file=sys.stderr)
        return 2

    log_path = project / "strip_lighting.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("strip")

    refs = patched = 0
    for scene in scenes.rglob("*.unity"):
        n = patch_scene(scene, args.dry_run, log)
        if n:
            patched += 1
            refs += n
    log.info("scenes patched: %d, refs cleared: %d", patched, refs)

    deleted = delete_lighting_files(scenes, args.dry_run, log)
    log.info("files deleted: %d", deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
