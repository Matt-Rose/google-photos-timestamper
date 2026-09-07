"""Survey a downloaded album folder and report what needs work before import.

Answers, for a folder of files pulled from Google Photos' web "Download all":

* how many files lack a usable capture date (the only ones needing recovery)
* which videos are VP9, and whether they are HDR (Photos silently refuses VP9)
* how many still+video pairs there are, which sets the expected album size
* the expected asset count, so an import can be verified by counting

Usage::

    python3 tools/album_survey.py "/path/to/Album Folder"

See docs/shared-album-reconciliation.md.
"""

import collections
import os
import subprocess
import sys

STILL_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
# Google's HLG HDR transfer function. Transcoding these to 8-bit while leaving
# the colour tags in place produces banding and wrong tone-mapping.
HLG_TRANSFER = "arib-std-b67"


def exif_survey(folder: str) -> list[dict[str, str]]:
    """One record per file: name, dims, both date tags, and video codec."""
    fmt = "$FileName|$ImageSize|$DateTimeOriginal|$CreateDate|$CompressorID"
    out = subprocess.run(
        ["exiftool", "-q", "-m", "-f", "-r", "-p", fmt, folder],
        capture_output=True,
        text=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        rows.append(dict(zip(("name", "dims", "dto", "create", "codec"), parts)))
    return rows


def has_usable_date(row: dict[str, str]) -> bool:
    for key in ("dto", "create"):
        value = row[key]
        if value in ("-", ""):
            continue
        # 0000:00:00 and 1970 both appear in real Takeout/Download data and
        # are worse than useless -- they sort a photo to the wrong end.
        if value.startswith("0000") or value[:4] == "1970":
            continue
        return True
    return False


def group_files(names: list[str]) -> dict[str, set[str]]:
    """Group by filename stem. A still and its video share a stem."""
    groups: dict[str, set[str]] = collections.defaultdict(set)
    for name in names:
        stem, ext = os.path.splitext(name)
        groups[stem].add(ext.lower())
    return groups


def is_pair(exts: set[str]) -> bool:
    return bool(exts & STILL_EXT) and bool(exts & VIDEO_EXT)


def hdr_videos(folder: str, names: list[str]) -> tuple[list[str], list[str]]:
    """Split VP9 files into (hdr, sdr). Requires ffprobe."""
    hdr, sdr = [], []
    for name in names:
        path = os.path.join(folder, name)
        transfer = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=color_transfer", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        ).stdout.strip()
        (hdr if transfer == HLG_TRANSFER else sdr).append(name)
    return hdr, sdr


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    folder = sys.argv[1]
    rows = exif_survey(folder)
    names = [r["name"] for r in rows]
    groups = group_files(names)
    pairs = [s for s, e in groups.items() if is_pair(e)]
    undated = [r for r in rows if not has_usable_date(r)]
    vp9 = [r["name"] for r in rows if r["codec"] == "vp09"]

    print(f"folder: {folder}")
    print(f"  files:                      {len(rows)}")
    print(f"  filename groups:            {len(groups)}")
    print(f"  still+video pairs:          {len(pairs)}")
    print(f"  EXPECTED ALBUM ASSETS:      {len(groups)}")
    print(f"  files lacking a date:       {len(undated)}")
    codecs = collections.Counter(r["codec"] for r in rows if r["codec"] not in ("-", ""))
    if codecs:
        print(f"  video codecs:               {dict(codecs)}")
    if vp9:
        hdr, sdr = hdr_videos(folder, vp9)
        print(f"  VP9 needing transcode:      {len(vp9)}  ({len(hdr)} HDR, {len(sdr)} SDR)")
        print("    -> Photos imports VP9 as NOTHING, silently. Transcode first.")
    if undated:
        print("\n  undated files (first 20):")
        for r in undated[:20]:
            print(f"    {r['name']:<50} {r['dims']}")


if __name__ == "__main__":
    main()
