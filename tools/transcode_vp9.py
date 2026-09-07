"""Transcode VP9 videos so Apple Photos will accept them, preserving HDR.

Photos refuses VP9 **silently** -- the file imports as nothing at all, with no
error. Google returns some downloads as VP9, sometimes inside a ``.MOV``
container, so the extension does not tell you the codec.

The trap: a naive H.264 encode of an HLG HDR source yields 8-bit output that
still carries ``bt2020`` / ``arib-std-b67`` colour tags, so players tone-map
data that has already been crushed -- banding and wrong brightness. HDR sources
must go to 10-bit HEVC with the tags preserved.

Originals are moved aside rather than overwritten, and the capture date is
re-asserted afterwards because re-encoding does not reliably carry it.

Usage::

    python3 tools/transcode_vp9.py <album-dir> --backup-dir <dir> [--dry-run]

See docs/shared-album-reconciliation.md.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

HLG_TRANSFER = "arib-std-b67"
VIDEO_EXT = {".mp4", ".mov", ".m4v"}


def probe(path: str, entries: str, stream: str | None = None) -> str:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", entries, "-of", "csv=p=0", path]
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def find_vp9(folder: str) -> list[str]:
    found = []
    for name in sorted(os.listdir(folder)):
        if os.path.splitext(name)[1].lower() not in VIDEO_EXT:
            continue
        path = os.path.join(folder, name)
        if probe(path, "stream=codec_name", "v").split("\n")[0] == "vp9":
            found.append(name)
    return found


def encode_cmd(src: str, dst: str, is_hdr: bool) -> list[str]:
    common = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if is_hdr:
        # 10-bit HEVC keeps the bit depth; ffmpeg carries the colour tags
        # through, and hvc1 is the tag Apple needs to play it natively.
        video = ["-c:v", "libx265", "-crf", "20", "-preset", "medium",
                 "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]
    else:
        video = ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                 "-pix_fmt", "yuv420p"]
    return common + video + ["-c:a", "copy", "-map_metadata", "0",
                             "-movflags", "+faststart", dst]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album_dir")
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from main import write_exif_tags

    names = find_vp9(args.album_dir)
    print(f"{len(names)} VP9 files in {args.album_dir}")
    os.makedirs(args.backup_dir, exist_ok=True)
    done = failed = 0

    for i, name in enumerate(names, 1):
        src = os.path.join(args.album_dir, name)
        is_hdr = probe(src, "stream=color_transfer", "v") == HLG_TRANSFER
        # Read the date BEFORE encoding; a stale value captured earlier in a
        # run silently skips the re-assertion afterwards.
        before = subprocess.run(["exiftool", "-s3", "-m", "-CreateDate", src],
                                capture_output=True, text=True).stdout.strip()
        label = "HDR->hevc10" if is_hdr else "SDR->h264"
        if args.dry_run:
            print(f"[{i}/{len(names)}] {name:<40} {label} (dry run)")
            continue

        tmp = src + ".transcode.tmp" + os.path.splitext(name)[1]
        duration = float(probe(src, "format=duration") or 0)
        result = subprocess.run(encode_cmd(src, tmp, is_hdr), capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(tmp):
            print(f"[{i}/{len(names)}] FAIL {name}: {result.stderr[:100]}")
            failed += 1
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        new_duration = float(probe(tmp, "format=duration") or 0)
        if abs(new_duration - duration) > 0.5 or os.path.getsize(tmp) < 10_000:
            print(f"[{i}/{len(names)}] FAIL {name}: verify ({duration} -> {new_duration})")
            os.remove(tmp)
            failed += 1
            continue

        shutil.move(src, os.path.join(args.backup_dir, name))
        os.rename(tmp, src)
        if before and not before.startswith("0000"):
            when = datetime.strptime(before, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
            write_exif_tags(src, when.timestamp(), None, None, None)
            os.utime(src, (when.timestamp(),) * 2)
        done += 1
        print(f"[{i}/{len(names)}] {name:<40} {label}")

    print(f"\ntranscoded={done} failed={failed}; originals in {args.backup_dir}")


if __name__ == "__main__":
    main()
