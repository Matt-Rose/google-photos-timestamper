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

    print(f"{'batch':<26}{'in':>8}{'kept':>8}{'dropped':>9}")
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

        with open(os.path.join(args.out, name), "w", encoding="utf-8") as handle:
            handle.writelines(p + "\n" for p in deduped)
        all_kept.extend(deduped)
        print(f"{name:<26}{len(lines):>8}{len(deduped):>8}{len(dropped):>9}")

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

    if args.trial:
        trial = trial_selection(all_kept, args.trial)
        path = os.path.join(args.out, "00-trial.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(p + "\n" for p in trial)
        stills = sum(1 for p in trial if os.path.splitext(p)[1].lower() in STILL_EXT)
        videos = sum(1 for p in trial if os.path.splitext(p)[1].lower() in VIDEO_EXT)
        pairs = sum(1 for _, g in collections.Counter(
            group_stem(p) for p in trial).items() if g > 1)
        print(f"\ntrial list: {len(trial)} files ({stills} stills, {videos} videos, "
              f"{pairs} pairs) -> {path}")

    print(f"\nfiltered lists in {args.out}")


if __name__ == "__main__":
    main()
