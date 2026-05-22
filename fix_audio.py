#!/usr/bin/env python3
"""Re-encode .wav files through ffmpeg to fix decoding errors.

AssetRipper writes wav files with zeroed chunk sizes in the RIFF header, which
makes Unity's FSBTool fail with "Failed decoding audio clip". ffmpeg reads them
with -ignore_length and writes a valid wav. Originals are kept as .wav.bak.
"""

import argparse
import concurrent.futures
import logging
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple


def is_header_broken(path: Path) -> bool:
    """True when a RIFF/WAVE file has a zero RIFF size or zero data chunk size."""
    try:
        with path.open("rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            if struct.unpack("<I", head[4:8])[0] == 0:
                return True
            f.seek(12)
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    return False
                cid, csize = chunk[:4], struct.unpack("<I", chunk[4:8])[0]
                if cid == b"data":
                    return csize == 0
                if csize == 0:
                    return True
                f.seek(csize, 1)
    except Exception:
        return False


def reencode(ffmpeg: str, src: Path, backup: bool, force: bool) -> Tuple[Path, bool, str]:
    if not force and not is_header_broken(src):
        return (src, True, "skip-ok")

    tmp = src.with_suffix(".wav.fixed.tmp")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-ignore_length", "1", "-i", str(src),
           "-c:a", "pcm_s16le", "-f", "wav", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return (src, False, "timeout")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return (src, False, f"exec-failed: {exc}")

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 44:
        err = (proc.stderr or "").strip().replace("\n", " | ")[:400]
        tmp.unlink(missing_ok=True)
        return (src, False, f"ffmpeg-failed rc={proc.returncode}: {err}")

    if backup:
        bak = src.with_suffix(".wav.bak")
        if not bak.exists():
            try:
                shutil.copy2(src, bak)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                return (src, False, f"backup-failed: {exc}")

    try:
        os.replace(tmp, src)
    except Exception as exc:
        return (src, False, f"replace-failed: {exc}")
    return (src, True, "fixed")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="re-encode broken wav files via ffmpeg")
    parser.add_argument("--project", required=True, help="Unity project root")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg.exe")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--force", action="store_true", help="re-encode even valid files")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    if not (project / "Assets").exists():
        print(f"not a Unity project: {project}", file=sys.stderr)
        return 2
    if shutil.which(args.ffmpeg) is None and not Path(args.ffmpeg).exists():
        print(f"ffmpeg not found: {args.ffmpeg}", file=sys.stderr)
        return 2

    log_path = project / "fix_audio.log"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    log = logging.getLogger("audio")

    wavs = sorted((project / "Assets").rglob("*.wav"))
    broken = [p for p in wavs if is_header_broken(p)]
    targets = wavs if args.force else broken
    log.info("wav files: %d, broken header: %d, to process: %d",
             len(wavs), len(broken), len(targets))

    if args.dry_run:
        for p in targets[:20]:
            log.info("[dry] would re-encode %s", p)
        return 0
    if not targets:
        log.info("nothing to do")
        return 0

    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(reencode, args.ffmpeg, p, not args.no_backup, args.force): p
                for p in targets}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            path, success, msg = fut.result()
            if success:
                ok += 1
                log.info("[%d/%d] ok   %s (%s)", i, len(targets), path.name, msg)
            else:
                fail += 1
                log.error("[%d/%d] fail %s - %s", i, len(targets), path.name, msg)

    log.info("done: %d ok, %d failed", ok, fail)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
