"""Recover capture dates for album files that have none, from a reference tree.

Google's web "Download all" preserves EXIF for ~95% of files; the remainder
were dateless at source (Picasa-era uploads, WhatsApp forwards, screenshots).
This recovers those from an existing Takeout tree, cheapest method first:

1. **Filename** -- ``PXL_20230531_145342087``, ``IMG-20180225-WA0014``,
   ``...BURST20190804121948...`` encode the timestamp directly.
2. **Filename + aspect ratio** against the reference tree. Aspect ratio, not
   exact dimensions: the download is often a downscale of the same image. A
   mismatch is a rejection (it correctly rejects crops).
3. **Pixel comparison** for what remains -- a 64x64 contrast-normalised
   greyscale signature, RMS distance. Resolves counter collisions
   (``IMG_4853.JPG`` existing in several years) that names cannot.

For 2 and 3, **accept when all close candidates agree on the date; do not
require the best match to beat the runner-up.** Takeout duplicates a file into
both a year folder and an album folder, so near-identical runners-up are
expected -- a uniqueness test wrongly discards a 0.000 match sitting beside a
0.007 duplicate.

Usage::

    python3 tools/recover_dates.py survey  <album-dir> <reference-tree> -o proposals.csv
    python3 tools/recover_dates.py apply   proposals.csv          # writes EXIF

Requires Pillow for step 3. See docs/shared-album-reconciliation.md.
"""

import argparse
import collections
import csv
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ASPECT_TOLERANCE = 0.01
PIXEL_RMS_MAX = 0.45          # above this the images are simply different
PIXEL_CLOSE_BAND = 0.15       # candidates within this of the best are "close"

FILENAME_PATTERNS = [
    (re.compile(r"(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"), "datetime"),
    (re.compile(r"BURST(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})"), "burst"),
    (re.compile(r"(20\d{2})(\d{2})(\d{2})"), "date-only"),
]


def date_from_filename(name: str) -> str | None:
    for pattern, _kind in FILENAME_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        g = match.groups()
        if len(g) == 6:
            return f"{g[0]}-{g[1]}-{g[2]} {g[3]}:{g[4]}:{g[5]}"
        return f"{g[0]}-{g[1]}-{g[2]} 12:00:00"
    return None


def aspect(width: object, height: object) -> float | None:
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return None
    if not w or not h:
        return None
    return round(min(w, h) / max(w, h), 3)


def exif_date(path: str) -> str | None:
    """First plausible date from a file, as 'YYYY:MM:DD HH:MM:SS'."""
    out = subprocess.run(
        ["exiftool", "-s3", "-m", "-DateTimeOriginal", "-CreateDate", path],
        capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("0000") and line[:4] != "1970":
            return line
    return None


def signature(path: str, size: int = 64) -> list[float] | None:
    """Contrast-normalised greyscale thumbnail, so downscales still match."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        image = ImageOps.exif_transpose(Image.open(path)).convert("L")
        pixels = list(image.resize((size, size), Image.LANCZOS).getdata())
    except Exception:
        return None
    mean = sum(pixels) / len(pixels)
    sd = (sum((p - mean) ** 2 for p in pixels) / len(pixels)) ** 0.5 or 1.0
    return [(p - mean) / sd for p in pixels]


def rms(a: list[float], b: list[float]) -> float:
    return (sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)) ** 0.5


def index_reference(tree: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = collections.defaultdict(list)
    for root, _dirs, files in os.walk(tree):
        for name in files:
            index[name.lower()].append(os.path.join(root, name))
    return index


def survey(album_dir: str, tree: str, out_path: str) -> None:
    index = index_reference(tree)
    rows = []
    for name in sorted(os.listdir(album_dir)):
        if name.startswith("."):
            continue
        path = os.path.join(album_dir, name)
        if exif_date(path):
            continue                                   # already dated

        stamp = date_from_filename(name)
        if stamp:
            rows.append((album_dir, name, stamp, "filename", ""))
            continue

        candidates = index.get(name.lower(), [])
        if not candidates:
            rows.append((album_dir, name, "", "no-candidate", ""))
            continue

        src_sig = signature(path)
        target = aspect(*subprocess.run(
            ["exiftool", "-s3", "-ImageWidth", "-ImageHeight", path],
            capture_output=True, text=True).stdout.split()[:2] or [0, 0])

        scored = []
        for cand in candidates:
            cw, ch = (subprocess.run(
                ["exiftool", "-s3", "-ImageWidth", "-ImageHeight", cand],
                capture_output=True, text=True).stdout.split() + ["0", "0"])[:2]
            cand_aspect = aspect(cw, ch)
            if target and cand_aspect and abs(cand_aspect - target) > ASPECT_TOLERANCE:
                continue                                # different framing: reject
            score = None
            if src_sig:
                cand_sig = signature(cand)
                score = rms(src_sig, cand_sig) if cand_sig else None
            scored.append((score, cand))

        if not scored:
            rows.append((album_dir, name, "", "aspect-mismatch", ""))
            continue

        with_scores = [s for s in scored if s[0] is not None]
        if with_scores:
            best = min(s[0] for s in with_scores)
            if best > PIXEL_RMS_MAX:
                rows.append((album_dir, name, "", "no-pixel-match", f"{best:.3f}"))
                continue
            close = [c for s, c in with_scores if s <= best + PIXEL_CLOSE_BAND]
        else:
            close = [c for _s, c in scored]

        dates = {d[:10] for d in (exif_date(c) for c in close) if d}
        if len(dates) == 1:
            rows.append((album_dir, name, exif_date(close[0]), "matched", close[0]))
        else:
            rows.append((album_dir, name, "", f"date-conflict:{sorted(dates)}", ""))

    with open(out_path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["album_dir", "filename", "capture_date", "verdict", "source"])
        writer.writerows(rows)
    counts = collections.Counter(r[3].split(":")[0] for r in rows)
    print(f"wrote {out_path}: {len(rows)} undated files")
    for verdict, n in counts.most_common():
        print(f"  {n:>5}  {verdict}")


def apply(csv_path: str) -> None:
    from main import write_exif_tags

    ok = failed = 0
    for row in csv.DictReader(open(csv_path)):
        stamp = row["capture_date"]
        if not stamp:
            continue
        path = os.path.join(row["album_dir"], row["filename"])
        if not os.path.exists(path):
            print(f"MISSING {path}")
            failed += 1
            continue
        text = stamp.replace(":", "-", 2) if stamp[4] == ":" else stamp
        when = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        ts = when.timestamp()
        try:
            write_exif_tags(path, ts, None, None, None)
        except subprocess.CalledProcessError as exc:
            # exiftool refuses to write when the extension contradicts the
            # content (e.g. a JPEG named .png); -m does NOT rescue it. Rename.
            kind = subprocess.run(["file", "-b", path], capture_output=True, text=True).stdout
            if kind.startswith("JPEG") and path.lower().endswith(".png"):
                renamed = os.path.splitext(path)[0] + ".jpg"
                os.rename(path, renamed)
                write_exif_tags(renamed, ts, None, None, None)
                os.utime(renamed, (ts, ts))
                print(f"RENAMED {row['filename']} -> {os.path.basename(renamed)} (was a JPEG)")
                ok += 1
                continue
            print(f"FAIL {row['filename']}: {exc.stderr[:80]!r}")
            failed += 1
            continue
        os.utime(path, (ts, ts))   # AFTER exiftool: -overwrite_original resets mtime
        ok += 1
    print(f"applied ok={ok} failed={failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey")
    s.add_argument("album_dir")
    s.add_argument("reference_tree")
    s.add_argument("-o", "--out", default="date-proposals.csv")
    a = sub.add_parser("apply")
    a.add_argument("csv_path")
    args = parser.parse_args()
    if args.cmd == "survey":
        survey(args.album_dir, args.reference_tree, args.out)
    else:
        apply(args.csv_path)


if __name__ == "__main__":
    main()
