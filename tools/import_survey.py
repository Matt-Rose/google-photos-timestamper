"""Survey a large import set for the things that break an osxphotos import.

Three problems cost real failures on earlier runs, and all three are visible
from file metadata, so this finds them in ONE exiftool pass rather than three
walks over the data:

* **A capture date Photos rejects** fails the entire chunk it is in, not just
  the one file -- so a single absurd date can cost thousands of imports.
* **An extension that contradicts the content** makes exiftool refuse to
  write, which blocks date repair. `-m` does not rescue it.
* **A codec Photos silently refuses**, VP9 being the one that bit before.
  Rather than guessing what VP9 is called, this tallies every codec it sees
  so an unexpected one cannot hide.

    python3 tools/import_survey.py LIST.txt [LIST.txt ...] --out DIR

Resumable: results are appended per chunk and re-read on start, so an
interrupted run costs only the chunk it was in.
"""

import argparse
import collections
import json
import os
import subprocess
import sys
import tempfile

MIN_YEAR, MAX_YEAR = 1900, 2100

# Output is read as JSON, not exiftool's -T tabular mode. -T strips trailing
# whitespace from every value, so a directory named "Kefalonia " comes back as
# "Kefalonia" and the reconstructed path does not exist. Google's export
# contains plenty of such folders -- 13 of them, holding 1,113 files, in one
# real set. -T also cannot emit SourceFile at all (it returns "-"), so there is
# no way to recover the true path from it. JSON gives SourceFile back verbatim.
TAGS = ["-SourceFile", "-FileType", "-FileTypeExtension",
        "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate", "-CompressorID"]

# Extensions that legitimately disagree with the detected type. These are
# containers that share a format, not mislabelled files: a .MOV holding an
# MP4-branded ISO-BMFF stream is normal and Photos reads it happily. Flagging
# them would bury the real mismatches -- JPEG bytes under a .HEIC name -- in
# tens of thousands of false positives.
EQUIVALENT = [
    {"jpg", "jpeg"},
    {"mp4", "mov", "m4v", "3gp", "3g2"},
    {"heic", "heif"},
    {"tif", "tiff"},
]


def equivalent(actual: str, claimed: str) -> bool:
    if actual == claimed:
        return True
    return any(actual in group and claimed in group for group in EQUIVALENT)


def date_problem(dates: list[str]) -> str | None:
    """None if some date is usable, else why not.

    The two answers are NOT equally serious and must not be reported together:

    * ``no date`` is benign. Photos imports the file and dates it from the
      filesystem. At this scale there are thousands, and lumping them in
      would bury the ones that matter.
    * ``unusable date`` is dangerous. Photos rejects an out-of-range capture
      date outright and fails the WHOLE CHUNK it is in, so one such file can
      cost thousands of imports.
    """
    seen = [d for d in dates if d and d != "-"]
    if not seen:
        return "no date"
    for value in seen:
        head = value.split(" ")[0].split("+")[0]
        parts = head.split(":")
        if len(parts) < 3:
            continue
        try:
            year, month, day = (int(p) for p in parts[:3])
        except ValueError:
            continue
        if MIN_YEAR <= year <= MAX_YEAR and 1 <= month <= 12 and 1 <= day <= 31:
            return None
    return f"unusable date ({seen[0]})"


def read_lists(paths: list[str]) -> list[str]:
    files, seen = [], set()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                name = line.strip()
                if name and name not in seen:
                    seen.add(name)
                    files.append(name)
    return files


def run_exiftool(chunk: list[str]) -> list[dict]:
    """One exiftool invocation over a chunk, returned as dicts.

    Files are passed via an argfile because a chunk of several thousand paths
    will not fit in a command line, and because it handles spaces and quotes
    without shell escaping.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as handle:
        handle.write("\n".join(chunk))
        argfile = handle.name
    try:
        proc = subprocess.run(
            ["exiftool", "-@", argfile, "-j", *TAGS],
            capture_output=True, text=True, errors="replace")
        if not proc.stdout.strip():
            return []
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            print(f"  WARNING: unparseable exiftool output for a chunk of "
                  f"{len(chunk)}", file=sys.stderr, flush=True)
            return []
    finally:
        os.unlink(argfile)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lists", nargs="+", help="file lists to survey")
    parser.add_argument("--out", required=True, help="directory for the report")
    parser.add_argument("--chunk", type=int, default=2000)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    results = os.path.join(args.out, "survey.tsv")

    done = set()
    if os.path.exists(results):
        with open(results, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                done.add(line.split("\t")[0])
        print(f"resuming: {len(done)} already surveyed", file=sys.stderr)

    files = read_lists(args.lists)
    todo = [f for f in files if f not in done]
    print(f"{len(files)} files, {len(todo)} to survey", file=sys.stderr)

    with open(results, "a", encoding="utf-8") as out:
        for start in range(0, len(todo), args.chunk):
            chunk = todo[start:start + args.chunk]
            for row in run_exiftool(chunk):
                path = row.get("SourceFile", "")
                if not path:
                    continue
                ftype = str(row.get("FileType", "-"))
                fext = str(row.get("FileTypeExtension", "-"))
                codec = str(row.get("CompressorID", "-"))
                claimed = os.path.splitext(path)[1].lstrip(".").lower()
                mismatch = "" if equivalent(fext.lower(), claimed) else f"{claimed}->{fext}"
                dates = [str(row.get(t, "")) for t in
                         ("DateTimeOriginal", "CreateDate", "MediaCreateDate")]
                problem = date_problem(dates) or ""
                out.write(f"{path}\t{ftype}\t{mismatch}\t{problem}\t{codec}\n")
            out.flush()
            print(f"  {min(start + args.chunk, len(todo))}/{len(todo)}",
                  file=sys.stderr, flush=True)

    # --- report ---------------------------------------------------------
    codecs, mismatches = collections.Counter(), []
    undated, bad_dates = [], []
    total = 0
    with open(results, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            total += 1
            path, _ftype, mismatch, problem, codec = parts[:5]
            if mismatch:
                mismatches.append(f"{path}\t{mismatch}")
            if problem == "no date":
                undated.append(path)
            elif problem:
                bad_dates.append(f"{path}\t{problem}")
            if codec and codec != "-":
                codecs[codec] += 1

    def dump(name, rows):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + ("\n" if rows else ""))

    dump("extension-mismatches.txt", mismatches)
    dump("undated.txt", undated)
    dump("bad-dates.txt", bad_dates)

    print(f"\nsurveyed {total} files")
    print(f"\nBLOCKING -- fails the whole chunk it is in:")
    print(f"  unusable capture date       : {len(bad_dates)}")
    print(f"\ninformational:")
    print(f"  no capture date (benign)    : {len(undated)}")
    print(f"  extension contradicts content: {len(mismatches)}")
    print(f"    (Photos sniffs content and imports these fine; it only blocks")
    print(f"     exiftool from writing, so it matters only for date repair)")
    print("\nvideo codecs seen:")
    for codec, n in codecs.most_common():
        print(f"  {codec:<12}{n:>8}")
    print(f"\nlists written to {args.out}")


if __name__ == "__main__":
    main()
