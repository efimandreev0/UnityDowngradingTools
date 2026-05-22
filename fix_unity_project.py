#!/usr/bin/env python3
"""Downgrade an AssetRipper project to the Unity 2018.2 serialization format.

AssetRipper exports scenes and prefabs in the 2018.3+ format, which makes an
older Unity fail with "Do not use ReadObjectThreaded on scene objects!".

Walks every .unity/.prefab/.asset under Assets/ and ProjectSettings/ and applies
format fixes. Idempotent.
"""

import argparse
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple


@dataclass
class Fix:
    name: str
    desc: str
    apply: Callable[[bytes], Tuple[bytes, int]]
    extensions: Tuple[str, ...] = (".unity", ".prefab", ".asset")


def _sub(pattern: re.Pattern, repl, data: bytes) -> Tuple[bytes, int]:
    return pattern.subn(repl, data)


# serializedVersion: 2018.2 uses RenderSettings 8, LightmapSettings 9, GameObject 5.
RENDERSETTINGS_VER = re.compile(
    rb"(--- !u!104 [^\n]*\nRenderSettings:\n(?:[^\n]*\n)*?  serializedVersion: )9")
LIGHTMAPSETTINGS_VER = re.compile(
    rb"(--- !u!157 [^\n]*\nLightmapSettings:\n(?:[^\n]*\n)*?  serializedVersion: )(?:10|11)")
GAMEOBJECT_VER = re.compile(rb"(--- !u!1 [^\n]*\nGameObject:\n  serializedVersion: )6")


def fix_rendersettings_version(data: bytes) -> Tuple[bytes, int]:
    return _sub(RENDERSETTINGS_VER, rb"\g<1>8", data)


def fix_lightmapsettings_version(data: bytes) -> Tuple[bytes, int]:
    return _sub(LIGHTMAPSETTINGS_VER, rb"\g<1>9", data)


def fix_gameobject_version(data: bytes) -> Tuple[bytes, int]:
    return _sub(GAMEOBJECT_VER, rb"\g<1>5", data)


CORRESPONDING_SRC = re.compile(rb"m_CorrespondingSourceObject:")
PREFAB_INSTANCE_FIELD = re.compile(rb"^( +)m_PrefabInstance:( .*)$", re.MULTILINE)
PREFAB_ASSET_FIELD = re.compile(rb"^( +)m_PrefabAsset: \{fileID: 0\}\n", re.MULTILINE)


def fix_corresponding_source(data: bytes) -> Tuple[bytes, int]:
    return _sub(CORRESPONDING_SRC, b"m_PrefabParentObject:", data)


def fix_prefab_instance_field(data: bytes) -> Tuple[bytes, int]:
    return _sub(PREFAB_INSTANCE_FIELD, rb"\g<1>m_PrefabInternal:\g<2>", data)


def fix_prefab_asset_field(data: bytes) -> Tuple[bytes, int]:
    return _sub(PREFAB_ASSET_FIELD, b"", data)


# Fields below only exist in 2018.3+ and crash the 2018.2 reader. Strip them,
# scoped to the component blocks they belong to.

CAMERA_PHYSICAL_FIELDS = re.compile(
    rb"^  (m_projectionMatrixMode|m_SensorSize|m_LensShift|m_FocalLength|m_GateFitMode):.*\n",
    re.MULTILINE)
SPRITERENDERER_NEW_FIELDS = re.compile(
    rb"^  (m_DynamicOccludee|m_RenderingLayerMask):.*\n", re.MULTILINE)
MESHRENDERER_NEW_FIELDS = re.compile(
    rb"^  (m_DynamicOccludee|m_RenderingLayerMask):.*\n", re.MULTILINE)
LIGHT_NEW_FIELDS = re.compile(
    rb"^  (m_RenderingLayerMask|m_BoundingSphereOverride|m_UseBoundingSphereOverride):.*\n",
    re.MULTILINE)


def _strip_in_blocks(data: bytes, class_prefixes: Tuple[bytes, ...],
                     field_re: re.Pattern) -> Tuple[bytes, int]:
    parts = re.split(rb"(?m)^(--- !u!\d+ .*\n)", data)
    out = [parts[0]]
    total = 0
    i = 1
    while i < len(parts):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else b""
        if any(header.startswith(p) for p in class_prefixes):
            body, n = field_re.subn(b"", body)
            total += n
        out.append(header)
        out.append(body)
        i += 2
    return b"".join(out), total


def fix_camera_physical_fields(data: bytes) -> Tuple[bytes, int]:
    return _strip_in_blocks(data, (b"--- !u!20 ",), CAMERA_PHYSICAL_FIELDS)


def fix_spriterenderer_new_fields(data: bytes) -> Tuple[bytes, int]:
    return _strip_in_blocks(data, (b"--- !u!212 ",), SPRITERENDERER_NEW_FIELDS)


def fix_meshrenderer_new_fields(data: bytes) -> Tuple[bytes, int]:
    return _strip_in_blocks(data, (b"--- !u!23 ", b"--- !u!137 "), MESHRENDERER_NEW_FIELDS)


def fix_light_new_fields(data: bytes) -> Tuple[bytes, int]:
    return _strip_in_blocks(data, (b"--- !u!108 ",), LIGHT_NEW_FIELDS)


PREFAB_INSTANCE_BLOCK = re.compile(rb"^PrefabInstance:\n", re.MULTILINE)


def fix_prefabinstance_block(data: bytes) -> Tuple[bytes, int]:
    return _sub(PREFAB_INSTANCE_BLOCK, b"Prefab:\n", data)


FIXES_2018_2: List[Fix] = [
    Fix("rendersettings_ver", "RenderSettings serializedVersion 9 -> 8", fix_rendersettings_version),
    Fix("lightmapsettings_ver", "LightmapSettings serializedVersion 10/11 -> 9", fix_lightmapsettings_version),
    Fix("gameobject_ver", "GameObject serializedVersion 6 -> 5", fix_gameobject_version),
    Fix("camera_phys", "drop physical-camera fields", fix_camera_physical_fields),
    Fix("sprite_new", "drop SpriteRenderer 2018.3+ fields", fix_spriterenderer_new_fields),
    Fix("mesh_new", "drop MeshRenderer/SkinnedMeshRenderer 2018.3+ fields", fix_meshrenderer_new_fields),
    Fix("light_new", "drop Light 2018.3+ fields", fix_light_new_fields),
    Fix("prefab_instance_block", "PrefabInstance: -> Prefab:", fix_prefabinstance_block),
    Fix("corresponding_src", "m_CorrespondingSourceObject -> m_PrefabParentObject", fix_corresponding_source),
    Fix("prefab_instance_field", "m_PrefabInstance -> m_PrefabInternal", fix_prefab_instance_field),
    Fix("prefab_asset_field", "drop m_PrefabAsset", fix_prefab_asset_field),
]

FIXES_2018_4: List[Fix] = []


@dataclass
class Stats:
    files_seen: int = 0
    files_changed: int = 0
    per_fix: dict = field(default_factory=dict)


def walk_target_files(project_dir: Path):
    for root in (project_dir / "Assets", project_dir / "ProjectSettings"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in (".unity", ".prefab", ".asset"):
                yield path


def process_file(path: Path, fixes: List[Fix], dry_run: bool,
                 backup_dir: Optional[Path], log: logging.Logger, stats: Stats) -> bool:
    try:
        data = path.read_bytes()
    except Exception as exc:
        log.warning("read failed %s: %s", path, exc)
        return False
    if not data.startswith(b"%YAML"):
        return False

    original = data
    total = 0
    for fix in (f for f in fixes if path.suffix.lower() in f.extensions):
        data, n = fix.apply(data)
        if n:
            stats.per_fix[fix.name] = stats.per_fix.get(fix.name, 0) + n
            total += n
    if data == original:
        return False

    if dry_run:
        log.info("[dry] %s: %d changes", path, total)
        return True

    if backup_dir is not None:
        bak = backup_dir / path.relative_to(path.anchor)
        bak.parent.mkdir(parents=True, exist_ok=True)
        if not bak.exists():
            shutil.copy2(path, bak)
    else:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(path, bak)

    path.write_bytes(data)
    log.info("fixed %s: %d changes", path, total)
    return True


def normalize_project_version(project_dir: Path, target: str, dry_run: bool,
                              log: logging.Logger) -> None:
    pv = project_dir / "ProjectSettings" / "ProjectVersion.txt"
    if not pv.exists():
        return
    want = {"2018.2": "2018.2.21f1", "2018.4": "2018.4.36f1"}.get(target)
    if not want:
        return
    text = pv.read_text(encoding="utf-8")
    new_text = re.sub(r"m_EditorVersion:.*", f"m_EditorVersion: {want}", text)
    if new_text == text:
        return
    if dry_run:
        log.info("[dry] ProjectVersion.txt -> %s", want)
    else:
        shutil.copy2(pv, pv.with_suffix(".txt.bak"))
        pv.write_text(new_text, encoding="utf-8")
        log.info("ProjectVersion.txt set to %s", want)


def wipe_library(project_dir: Path, dry_run: bool, log: logging.Logger) -> None:
    lib = project_dir / "Library"
    if not lib.exists():
        return
    if dry_run:
        log.info("[dry] would remove %s", lib)
    else:
        log.info("removing Library/")
        shutil.rmtree(lib, ignore_errors=True)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Downgrade a Unity project to 2018.2")
    parser.add_argument("--project", required=True, help="Unity project root (contains Assets/)")
    parser.add_argument("--target", choices=["2018.2", "2018.4"], default="2018.2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--backup-dir", default=None,
                        help="directory for backups; default writes .bak next to each file")
    parser.add_argument("--wipe-library", action="store_true",
                        help="delete Library/ at the end (Unity must be closed)")
    parser.add_argument("--normalize-version", action="store_true",
                        help="rewrite m_EditorVersion in ProjectVersion.txt")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    if not (project / "Assets").exists():
        print(f"not a Unity project: {project}", file=sys.stderr)
        return 2

    log_path = project / "fix_unity_project.log"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("fix")

    fixes = FIXES_2018_2 if args.target == "2018.2" else FIXES_2018_4
    log.info("project: %s | target: %s | dry-run: %s", project, args.target, args.dry_run)

    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else None
    if backup_dir:
        backup_dir.mkdir(parents=True, exist_ok=True)

    stats = Stats()
    for path in walk_target_files(project):
        stats.files_seen += 1
        if process_file(path, fixes, args.dry_run, backup_dir, log, stats):
            stats.files_changed += 1

    if args.normalize_version:
        normalize_project_version(project, args.target, args.dry_run, log)

    log.info("files seen: %d, changed: %d", stats.files_seen, stats.files_changed)
    for name, n in sorted(stats.per_fix.items(), key=lambda kv: -kv[1]):
        log.info("  %-22s %d", name, n)

    if args.wipe_library:
        wipe_library(project, args.dry_run, log)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
