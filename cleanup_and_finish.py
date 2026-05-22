#!/usr/bin/env python3
"""Cleanup pass after fix_unity_project.py.

Removes .bak leftovers (Unity imports them as assets), applies the
m_CorrespondingSourceObject rename to .controller/.anim/.mat/.guiskin files that
the main pass skips, and lowers serializedVersion in GraphicsSettings.asset and
ProjectSettings.asset.
"""

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import List, Tuple

CORR_SRC = re.compile(rb"m_CorrespondingSourceObject:")
PREFAB_ASSET = re.compile(rb"^( +)m_PrefabAsset: \{fileID: 0\}\n", re.MULTILINE)
PREFAB_INSTANCE_FIELD = re.compile(rb"^( +)m_PrefabInstance:( .*)$", re.MULTILINE)

EXTRA_EXTS = (".controller", ".anim", ".mat", ".guiskin")


def cleanup_baks(project: Path, dry_run: bool, log: logging.Logger) -> int:
    n = 0
    for pat in ("*.bak", "*.bak2", "*.bak.meta", "*.bak2.meta", "*.txt.bak"):
        for f in project.rglob(pat):
            n += 1
            if dry_run:
                continue
            try:
                f.unlink()
            except Exception as exc:
                log.warning("rm failed %s: %s", f, exc)
    return n


def patch_extra_assets(project: Path, dry_run: bool) -> Tuple[int, int]:
    files_changed = total = 0
    for ext in EXTRA_EXTS:
        for f in (project / "Assets").rglob(f"*{ext}"):
            try:
                data = f.read_bytes()
            except Exception:
                continue
            if not data.startswith(b"%YAML"):
                continue
            data, n1 = CORR_SRC.subn(b"m_PrefabParentObject:", data)
            data, n2 = PREFAB_ASSET.subn(b"", data)
            data, n3 = PREFAB_INSTANCE_FIELD.subn(rb"\g<1>m_PrefabInternal:\g<2>", data)
            if n1 + n2 + n3 == 0:
                continue
            files_changed += 1
            total += n1 + n2 + n3
            if not dry_run:
                f.write_bytes(data)
    return files_changed, total


def patch_project_settings(project: Path, dry_run: bool, log: logging.Logger) -> int:
    targets = [
        (project / "ProjectSettings" / "GraphicsSettings.asset",
         re.compile(rb"(GraphicsSettings:\n  m_ObjectHideFlags: 0\n  serializedVersion: )12"),
         rb"\g<1>11"),
        (project / "ProjectSettings" / "ProjectSettings.asset",
         re.compile(rb"(PlayerSettings:\n  m_ObjectHideFlags: 0\n  serializedVersion: )15"),
         rb"\g<1>14"),
    ]
    n = 0
    for path, pat, repl in targets:
        if not path.exists():
            continue
        data = path.read_bytes()
        new_data, cnt = pat.subn(repl, data)
        if not cnt:
            continue
        n += cnt
        if dry_run:
            log.info("[dry] %s: serializedVersion lowered", path.name)
        else:
            path.write_bytes(new_data)
            log.info("patched %s", path.name)
    return n


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="cleanup pass")
    parser.add_argument("--project", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-baks", action="store_true",
                        help="do not delete .bak files")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    if not (project / "Assets").exists():
        print(f"not a Unity project: {project}", file=sys.stderr)
        return 2

    log_path = project / "cleanup_and_finish.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("cleanup")

    if not args.keep_baks:
        log.info("removed %d .bak files", cleanup_baks(project, args.dry_run, log))

    fc, total = patch_extra_assets(project, args.dry_run)
    log.info("extra assets changed: %d (%d edits)", fc, total)

    log.info("project settings edits: %d", patch_project_settings(project, args.dry_run, log))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
