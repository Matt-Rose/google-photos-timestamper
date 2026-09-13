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
    python3 tools/transcode_vp9.py --files vp9.txt --root <tree> --backup-dir <dir>

The ``--files`` form takes paths already known to be VP9 (from
``tools/import_survey.py``), which avoids re-probing every video in a large
tree. With it, backups mirror the source tree's directory structure -- across
a whole export basenames repeat (237 files sharing 114 names in one real set),
so a flat backup directory would silently overwrite originals.

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


# CRF for the HDR path. Measured on a real 6.4 Mbps VP9 source, SSIM against
# that source:
#
#     crf 20   24.9 MB (3.1x)   <- the old default
#     crf 24   15.8 MB (2.0x)   SSIM 0.9878
#     crf 26   12.3 MB (1.5x)   SSIM 0.9832
#
# The source is already a lossy Google re-encode, so crf 20 was spending three
# times its size preserving VP9's own compression artefacts. 24 keeps
# essentially the same SSIM for a third less data, which matters when every
# byte is also an iCloud upload.
#
# Hardware encoding was measured too and rejected: hevc_videotoolbox is 7x
# faster, but size-matched to the source it scored SSIM 0.9488 against 0.9878,
# and to match software quality it needed 3x the bytes. It does carry the HDR
# colour tags correctly, so it remains an option when time beats storage.
DEFAULT_HDR_CRF = 24


def encode_cmd(src: str, dst: str, is_hdr: bool, crf: int = DEFAULT_HDR_CRF) -> list[str]:
    common = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if is_hdr:
        # 10-bit HEVC keeps the bit depth; ffmpeg carries the colour tags
        # through, and hvc1 is the tag Apple needs to play it natively.
        # Verified on the output: bt2020nc / arib-std-b67 / bt2020 all survive.
        video = ["-c:v", "libx265", "-crf", str(crf), "-preset", "medium",
                 "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"]
    else:
        video = ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                 "-pix_fmt", "yuv420p"]
    return common + video + ["-c:a", "copy", "-map_metadata", "0",
                             "-movflags", "+faststart", dst]


def backup_path(src: str, root: str, backup_dir: str) -> str:
    """Where an original goes, mirroring its position under root.

    A flat backup directory is unsafe across a whole export: camera filenames
    repeat between folders, so the second IMG_5408.MOV would overwrite the
    first and its original would be gone for good.
    """
    rel = os.path.relpath(src, root) if root else os.path.basename(src)
    return os.path.join(backup_dir, rel)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album_dir", nargs="?", help="scan this folder for VP9")
    parser.add_argument("--files", help="list of known-VP9 paths, one per line")
    parser.add_argument("--root", help="tree root, so backups mirror its layout")
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--crf", type=int, default=DEFAULT_HDR_CRF,
                        help=f"HDR quality, lower is bigger (default {DEFAULT_HDR_CRF})")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from main import write_exif_tags

    if args.files:
        with open(args.files, encoding="utf-8") as handle:
            paths = [l.strip() for l in handle if l.strip()]
        root = args.root or ""
    elif args.album_dir:
        paths = [os.path.join(args.album_dir, n) for n in find_vp9(args.album_dir)]
        root = args.album_dir
    else:
        sys.exit("give either an album directory or --files")

    print(f"{len(paths)} VP9 files to transcode")
    os.makedirs(args.backup_dir, exist_ok=True)
    done = failed = 0

    for i, src in enumerate(paths, 1):
        name = os.path.basename(src)
        is_hdr = probe(src, "stream=color_transfer", "v") == HLG_TRANSFER
        # Read the date BEFORE encoding; a stale value captured earlier in a
        # run silently skips the re-assertion afterwards.
        before = subprocess.run(["exiftool", "-s3", "-m", "-CreateDate", src],
                                capture_output=True, text=True).stdout.strip()
        label = "HDR->hevc10" if is_hdr else "SDR->h264"
        if args.dry_run:
            print(f"[{i}/{len(paths)}] {name:<40} {label} (dry run)", flush=True)
            continue

        tmp = src + ".transcode.tmp" + os.path.splitext(name)[1]
        dest = backup_path(src, root, args.backup_dir)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        duration = float(probe(src, "format=duration") or 0)
        result = subprocess.run(encode_cmd(src, tmp, is_hdr, args.crf),
                                capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(tmp):
            print(f"[{i}/{len(paths)}] FAIL {name}: {result.stderr[:100]}", flush=True)
            failed += 1
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        new_duration = float(probe(tmp, "format=duration") or 0)
        if abs(new_duration - duration) > 0.5 or os.path.getsize(tmp) < 10_000:
            print(f"[{i}/{len(paths)}] FAIL {name}: verify ({duration} -> {new_duration})", flush=True)
            os.remove(tmp)
            failed += 1
            continue

        shutil.move(src, dest)
        os.rename(tmp, src)
        if before and not before.startswith("0000"):
            when = datetime.strptime(before, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
            write_exif_tags(src, when.timestamp(), None, None, None)
            os.utime(src, (when.timestamp(),) * 2)
        done += 1
        print(f"[{i}/{len(paths)}] {name:<40} {label}", flush=True)

    print(f"\ntranscoded={done} failed={failed}; originals in {args.backup_dir}")


if __name__ == "__main__":
    main()
