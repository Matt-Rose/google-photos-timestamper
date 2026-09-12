"""Find which files in an import tree are already in a Photos library.

Written for the bulk phase: a Takeout export of ~284k files where most are
duplicates of album files imported earlier. Handing the whole tree to
osxphotos with ``--skip-dups`` "works", but skipping is not free -- it still
hashes every file, and a run that long will be interrupted. Pruning first
turns one enormous unverifiable run into a cheap decision pass plus a short
import of only what is genuinely new.

Usage::

    # decide (writes a ledger, moves nothing)
    python3 tools/prune_imported.py SOURCE_DIR LIBRARY.photoslibrary

    # act on the decisions
    python3 tools/prune_imported.py SOURCE_DIR LIBRARY.photoslibrary --move-to DIR

Needs osxphotos importable, so run it with that virtualenv's python. It reads
the library with osxphotos' own read-only temp-copy helper and never writes to
it; ``--move-to`` is the only thing that touches the filesystem, and it moves
source files, never library files.

Four things this gets right that a naive version does not:

* **Build the index once.** ``FingerprintQuery`` copies the whole Photos.sqlite
  on construction. Per-file construction would copy a multi-GB database
  hundreds of thousands of times.
* **Measure before optimising.** osxphotos' own source says Photos hashes
  photos but not videos, which would make hashing a video pure waste -- it
  reads every byte to produce a value that can never match. On a Photos 11.1
  library that is simply not true any more: 91.3% of photos and 87.1% of
  videos carry a hash. So videos are hashed by default and ``--report-only``
  prints the real coverage, because the answer is version-dependent and the
  cost of getting it wrong is silently importing thousands of duplicates.
* **Always fall back to filename + size.** That coverage is not 100%, so some
  assets cannot be matched by hash at all. The fallback runs whenever the hash
  misses, not only for videos.
* **Prune by Live Photo group, not by file.** A still already in the library
  cannot be retrofitted into a Live Photo (see
  docs/shared-album-reconciliation.md), so importing its orphaned video adds a
  silent duplicate rather than motion. If the still is present, its video goes
  too.
* **Resume.** The ledger is flushed per line and re-read on start, so an
  interrupted pass costs only the files it had not yet reached.
"""

import argparse
import collections
import os
import shutil
import sys
import time

STILL_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff", ".dng"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi"}
MEDIA_EXT = STILL_EXT | VIDEO_EXT

# Ledger decisions.
PRESENT = "present"        # matched an asset in the library
ORPHAN = "orphan-video"    # video whose paired still is already in the library
NEW = "new"                # no match -- import this
FAILED = "failed"          # could not be read or hashed


def load_osxphotos():
    try:
        from osxphotos._constants import _DB_TABLE_NAMES
        from osxphotos.fingerprint import fingerprint
        from osxphotos.fingerprintquery import FingerprintQuery
    except ImportError:
        sys.exit(
            "osxphotos is not importable. Run this with the python from the "
            "virtualenv osxphotos is installed in, e.g.\n"
            "  path/to/venv/bin/python tools/prune_imported.py ..."
        )
    return FingerprintQuery, fingerprint, _DB_TABLE_NAMES


def hash_coverage(query, table_names) -> str:
    """How many assets of each kind actually carry a content hash.

    The column moved: it is ``ZMASTERFINGERPRINT`` up to Photos 9.6 and
    ``ZORIGINALSTABLEHASH`` from 9.9 on, which is why this asks osxphotos for
    the name rather than hard-coding one.
    """
    column = table_names[query.photos_version]["MASTER_FINGERPRINT"].split(".")[-1]
    rows = query.conn.execute(
        f"select x.ZKIND, count(*), "
        f"sum(case when aa.{column} is not null and aa.{column} != '' then 1 else 0 end) "
        "from ZASSET x join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET = x.Z_PK "
        "where x.ZTRASHEDSTATE = 0 group by x.ZKIND"
    ).fetchall()
    out = [f"library hash coverage (column {column}):"]
    for kind, total, hashed in rows:
        label = "video" if kind == 1 else "photo"
        pct = 100.0 * hashed / total if total else 0.0
        out.append(f"  {label:<6} {total:>7} assets, {hashed:>7} hashed ({pct:.1f}%)")
    return "\n".join(out)


def media_files(root: str) -> list[str]:
    """Every media file under root, as paths relative to it, sorted."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in MEDIA_EXT:
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    found.sort()
    return found


def read_ledger(path: str) -> dict[str, str]:
    """Decisions already made, so a re-run picks up where it stopped.

    Later lines win, which is what lets the orphan pass override an earlier
    ``new`` for the same file.
    """
    done: dict[str, str] = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                done[parts[0]] = parts[1]
    return done


def still_to_decide(files: list[str], decisions: dict[str, str]) -> list[str]:
    """Files needing a decision: undecided ones, plus previous failures.

    A failure is usually transient -- a disk hiccup on a 728 GB external drive,
    a file being written while the pass ran. Treating it as final would leave
    the file permanently undecided in the ledger and silently skipped by every
    resume, so failures are always retried.
    """
    return [f for f in files if decisions.get(f, FAILED) == FAILED]


def classify(query, fingerprint, root: str, rel: str, hash_videos: bool):
    """Return (decision, method, match) for one file."""
    full = os.path.join(root, rel)
    is_video = os.path.splitext(rel)[1].lower() in VIDEO_EXT
    try:
        if not (is_video and not hash_videos):
            hits = query.photos_by_fingerprint(fingerprint(full))
            if hits:
                return PRESENT, "hash", hits[0][0]
        size = os.path.getsize(full)
        hits = query.photos_by_filename_size(os.path.basename(rel), size)
        if hits:
            return PRESENT, "name+size", hits[0][0]
    except Exception as exc:                      # unreadable, truncated, gone
        return FAILED, type(exc).__name__, str(exc)[:120]
    return NEW, "", ""


def orphaned_videos(decisions: dict[str, str], files: list[str]) -> set[str]:
    """Videos whose paired still is already in the library.

    Pairing is by directory + filename stem, the same rule the chunker in
    tools/photos_import.py uses. Only a *still* being present orphans the
    video; a present video does not make a missing still unwanted.
    """
    present_stills = set()
    for rel, decision in decisions.items():
        stem, ext = os.path.splitext(rel)
        if decision == PRESENT and ext.lower() in STILL_EXT:
            present_stills.add(stem)
    return {
        rel for rel in files
        if os.path.splitext(rel)[1].lower() in VIDEO_EXT
        and decisions.get(rel) == NEW
        and os.path.splitext(rel)[0] in present_stills
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="tree of files staged for import")
    parser.add_argument("library", help="path to the .photoslibrary")
    parser.add_argument("--ledger", help="decision file (default: SOURCE/.prune-ledger.tsv)")
    parser.add_argument("--move-to", help="move already-present files here")
    parser.add_argument("--no-video-hash", action="store_true",
                        help="skip hashing videos; only sane if --report-only "
                             "shows the library does not hash them either")
    parser.add_argument("--report-only", action="store_true",
                        help="print library hash coverage and stop")
    parser.add_argument("--limit", type=int, help="stop after this many new decisions")
    args = parser.parse_args()

    FingerprintQuery, fingerprint, table_names = load_osxphotos()
    query = FingerprintQuery(args.library)
    print(hash_coverage(query, table_names), file=sys.stderr)
    if args.report_only:
        return
    if not os.path.isdir(args.source):
        sys.exit(f"no such directory: {args.source}")

    ledger_path = args.ledger or os.path.join(args.source, ".prune-ledger.tsv")
    decisions = read_ledger(ledger_path)
    files = media_files(args.source)
    todo = still_to_decide(files, decisions)
    print(f"\n{len(files)} media files, {len(todo)} still to decide", file=sys.stderr)

    started, done = time.time(), 0
    with open(ledger_path, "a", encoding="utf-8") as ledger:
        for rel in todo:
            if args.limit and done >= args.limit:
                break
            decision, method, match = classify(
                query, fingerprint, args.source, rel, not args.no_video_hash)
            decisions[rel] = decision
            ledger.write(f"{rel}\t{decision}\t{method}\t{match}\n")
            ledger.flush()
            done += 1
            if done % 500 == 0:
                rate = done / (time.time() - started)
                left = (len(todo) - done) / rate if rate else 0
                print(f"  {done}/{len(todo)}  {rate:.0f}/s  ~{left/60:.0f} min left",
                      file=sys.stderr)

        # Orphan detection needs the whole picture, so it runs once at the end.
        orphans = orphaned_videos(decisions, files)
        for rel in sorted(orphans):
            decisions[rel] = ORPHAN
            ledger.write(f"{rel}\t{ORPHAN}\tpaired-still-present\t\n")

    counts = collections.Counter(decisions.values())
    print(f"\n{'decision':<14} files")
    for decision in (NEW, PRESENT, ORPHAN, FAILED):
        print(f"{decision:<14} {counts.get(decision, 0)}")
    print(f"\nledger: {ledger_path}")

    if not args.move_to:
        print("dry run -- pass --move-to DIR to move the present/orphan files aside")
        return

    if os.path.exists(args.move_to) and (
        os.stat(args.move_to).st_dev != os.stat(args.source).st_dev
    ):
        print("warning: --move-to is on a different volume, so this copies "
              "rather than renames -- expect it to take hours", file=sys.stderr)

    moved = 0
    for rel, decision in sorted(decisions.items()):
        if decision not in (PRESENT, ORPHAN):
            continue
        src = os.path.join(args.source, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(args.move_to, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved += 1
    print(f"moved {moved} files to {args.move_to}")
    print(f"{counts.get(NEW, 0)} files remain in {args.source} for import")


if __name__ == "__main__":
    main()
