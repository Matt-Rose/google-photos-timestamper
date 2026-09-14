"""Filter osxphotos batch lists against a prune ledger.

The paced import harness works from batch files -- one list of absolute paths
per year-part, imported one batch at a time so iCloud can drain between them.
`prune_imported.py` decides which files are worth importing. This joins the
two, writing new batch lists holding only the files still worth importing.

    python3 tools/filter_batches.py BATCH_DIR TREE_ROOT --out OUT_DIR
    python3 tools/filter_batches.py BATCH_DIR TREE_ROOT --out OUT_DIR --trial 500

Filtering the lists is much better than moving the excluded files aside. The
lists hold absolute paths, so moving files invalidates every one of them, and
on a spinning disk it is tens of thousands of renames to achieve nothing the
list cannot express.

Two things it will not do quietly:

* **A line the ledger has never seen is KEPT**, and counted in the report by
  extension. The ledger only covers media extensions, so the batch lists also
  contain Motion Photo `.MP` sidecars and similar. Dropping a file the prune
  never examined would be guessing, and guessing in the direction of silent
  loss.
* **A trial selection never splits a Live Photo pair.** A still and its video
  share a stem; importing one without the other is how you get an unpaired
  half that can never be fixed afterwards.
"""

import argparse
import collections
import os
import sys

STILL_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff", ".dng"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi"}

KEEP = "new"          # the only decision worth importing

# makelive -- which osxphotos calls for --auto-live -- validates by EXTENSION
# only, and its whitelists are narrower than osxphotos' own content-sniffing
# check. So osxphotos hands it a pair that it then refuses, raising an
# uncaught ValueError that aborts the entire import before anything is
# brought in.
MAKELIVE_IMAGE_EXT = {".jpg", ".jpeg", ".heic", ".heif"}
MAKELIVE_VIDEO_EXT = {".mov", ".mp4"}


def read_decisions(ledger: str) -> dict[str, str]:
    """path -> final decision. Later lines win, as in prune_imported."""
    out: dict[str, str] = {}
    with open(ledger, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    return out


def relative_to_tree(path: str, root: str) -> str | None:
    """Ledger keys are paths relative to the tree root; batch lines are absolute."""
    root = root.rstrip(os.sep) + os.sep
    return path[len(root):] if path.startswith(root) else None


def filter_lines(lines, decisions: dict[str, str], root: str):
    """Split batch lines into (kept, dropped, unknown).

    unknown = not in the ledger at all. Those are kept, but reported
    separately so the decision to keep them is visible rather than implied.
    """
    kept, dropped, unknown = [], [], []
    for raw in lines:
        path = raw.strip()
        if not path:
            continue
        rel = relative_to_tree(path, root)
        decision = decisions.get(rel) if rel is not None else None
        if decision is None:
            unknown.append(path)
            kept.append(path)
        elif decision == KEEP:
            kept.append(path)
        else:
            dropped.append((path, decision))
    return kept, dropped, unknown


def group_stem(path: str) -> str:
    """Directory + filename stem: what a Live Photo's two halves share."""
    return os.path.splitext(path)[0]


def autolive_unsafe(paths: list[str]) -> set[str]:
    """Files that would crash `osxphotos import --auto-live`.

    A file is unsafe when it shares a directory and stem with a file of the
    other kind -- so osxphotos groups them as a Live Photo pair -- but its
    extension is outside makelive's whitelist. Measured on one real set: 3,195
    stills (3,194 .png and one .gif) and no videos.

    None of them is a real Live Photo. Apple stores a ContentIdentifier in both
    halves of a genuine pair and Google preserves it; not one of these carries
    it. They are filename-stem collisions -- a screenshot named IMG_4713.PNG
    and an unrelated IMG_4713.MOV -- so importing them without --auto-live
    loses nothing at all.

    Renaming them would be the wrong fix. Only 883 of the 3,195 are
    mislabelled (JPEG bytes under a .PNG name); the other 2,312 are genuinely
    PNG, and renaming those would introduce exactly the mislabelling this
    guards against.
    """
    by_stem: dict[str, list[str]] = collections.defaultdict(list)
    for path in paths:
        by_stem[group_stem(path)].append(path)
    unsafe: set[str] = set()
    for group in by_stem.values():
        exts = {os.path.splitext(p)[1].lower() for p in group}
        if not (exts & STILL_EXT and exts & VIDEO_EXT):
            continue                      # not a pair; nothing to validate
        for path in group:
            ext = os.path.splitext(path)[1].lower()
            if ext in STILL_EXT and ext not in MAKELIVE_IMAGE_EXT:
                unsafe.add(path)
            elif ext in VIDEO_EXT and ext not in MAKELIVE_VIDEO_EXT:
                unsafe.add(path)
    return unsafe


def trial_selection(kept: list[str], count: int) -> list[str]:
    """A small spread across the set, never splitting a still/video pair.

    Walks the kept files at a fixed stride rather than taking a prefix, so the
    sample spans years and folders instead of whatever sorts first. Whenever a
    file is chosen its whole stem-group comes too, so a Live Photo is imported
    as a pair or not at all.
    """
    if count >= len(kept):
        return list(kept)
    by_stem: dict[str, list[str]] = collections.defaultdict(list)
    for path in kept:
        by_stem[group_stem(path)].append(path)
    stems = sorted(by_stem)
    stride = max(1, len(stems) // max(1, count))
    chosen: list[str] = []
    for i in range(0, len(stems), stride):
        group = by_stem[stems[i]]
        if len(chosen) + len(group) > count and chosen:
            break
        chosen.extend(group)
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_dir", help="directory of existing .txt batch lists")
    parser.add_argument("tree_root", help="root the ledger paths are relative to")
    parser.add_argument("--ledger", help="default: TREE_ROOT/.prune-ledger.tsv")
    parser.add_argument("--out", required=True, help="directory to write filtered lists")
    parser.add_argument("--trial", type=int, help="also write a trial list of N files")
    parser.add_argument("--split-autolive", action="store_true",
                        help="hold back files that would crash --auto-live, "
                             "into 00-no-autolive.txt for a second pass")
    args = parser.parse_args()

    ledger = args.ledger or os.path.join(args.tree_root, ".prune-ledger.tsv")
    if not os.path.exists(ledger):
        sys.exit(f"no ledger at {ledger}")
    decisions = read_decisions(ledger)
    os.makedirs(args.out, exist_ok=True)

    names = sorted(f for f in os.listdir(args.batch_dir) if f.endswith(".txt"))
    if not names:
        sys.exit(f"no .txt batch lists in {args.batch_dir}")

    seen: dict[str, str] = {}
    dupes = 0
    totals = collections.Counter()
    unknown_ext = collections.Counter()
    all_kept: list[str] = []
    held_back: list[str] = []

    # Filter first, THEN decide what is unsafe. Pairing has to be judged on the
    # files actually being imported: a .png whose video partner was dropped as
    # an orphan is no longer half of a pair, so --auto-live never looks at it
    # and holding it back would be pointless. Judging before filtering held
    # back 3,431 files where only 3,195 are really at risk.
    per_batch: dict[str, list[str]] = {}
    for name in names:
        with open(os.path.join(args.batch_dir, name), encoding="utf-8",
                  errors="replace") as handle:
            lines = handle.readlines()
        kept, dropped, unknown = filter_lines(lines, decisions, args.tree_root)

        # A path listed in two batches would be imported twice.
        deduped = []
        for path in kept:
            if path in seen:
                dupes += 1
                continue
            seen[path] = name
            deduped.append(path)

        for path in unknown:
            unknown_ext[os.path.splitext(path)[1].lower() or "(none)"] += 1
        for _, decision in dropped:
            totals[decision] += 1
        per_batch[name] = deduped
        totals[f"__in__{name}"] = len(lines)

    unsafe: set[str] = set()
    if args.split_autolive:
        every = [p for batch in per_batch.values() for p in batch]
        unsafe = autolive_unsafe(every)

    print(f"{'batch':<26}{'in':>8}{'kept':>8}{'held':>7}")
    for name in names:
        deduped = [p for p in per_batch[name] if p not in unsafe]
        held_back.extend(p for p in per_batch[name] if p in unsafe)
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as handle:
            handle.writelines(p + "\n" for p in deduped)
        all_kept.extend(deduped)
        print(f"{name:<26}{totals.pop(f'__in__{name}'):>8}{len(deduped):>8}"
              f"{len(per_batch[name]) - len(deduped):>7}")

    print(f"\n{'TOTAL kept':<26}{len(all_kept):>8}")
    print("dropped by decision:")
    for decision, n in totals.most_common():
        print(f"  {decision:<24}{n:>8}")
    if dupes:
        print(f"\n{dupes} duplicate path(s) across batches, listed once")
    if unknown_ext:
        print("\nnot in the ledger, kept anyway (the prune never examined these):")
        for ext, n in unknown_ext.most_common():
            print(f"  {ext:<24}{n:>8}")

    if args.split_autolive:
        path = os.path.join(args.out, "00-no-autolive.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(p + "\n" for p in sorted(held_back))
        print(f"\n{len(held_back)} files held back -> {path}")
        print("  import these WITHOUT --auto-live; none is a real Live Photo")

    if args.trial:
        trial = trial_selection(all_kept, args.trial)
        path = os.path.join(args.out, "00-trial.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(p + "\n" for p in trial)
        stills = sum(1 for p in trial if os.path.splitext(p)[1].lower() in STILL_EXT)
        videos = sum(1 for p in trial if os.path.splitext(p)[1].lower() in VIDEO_EXT)
        # Only a still+video stem counts: two videos sharing a stem, or a file
        # plus its .MP sidecar, are not a Live Photo and do not merge.
        by_stem: dict[str, set[str]] = collections.defaultdict(set)
        for p_ in trial:
            by_stem[group_stem(p_)].add(os.path.splitext(p_)[1].lower())
        pairs = sum(1 for e in by_stem.values()
                    if e & STILL_EXT and e & VIDEO_EXT)
        media = stills + videos
        print(f"\ntrial list: {len(trial)} files ({stills} stills, {videos} videos, "
              f"{pairs} live-photo pairs) -> {path}")
        print(f"  expected new assets on import: {media} - {pairs} = {media - pairs}")

    print(f"\nfiltered lists in {args.out}")


if __name__ == "__main__":
    main()
