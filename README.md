# google-photos-timestamper

Using [Google Takeout](https://takeout.google.com/settings/takeout) you can export your photos saved in Google Photos to migrate them wherever else you want.
However, while on Google's servers, the metadata gets stripped from the image/video files themselves and put in separate JSON sidecar files.

This script puts that metadata back: it reads each photo/video's JSON sidecar and writes the real taken-time and GPS location back into the file's own EXIF (and, for videos, QuickTime) tags, then sorts everything into `ready/`, `sidecars/`, and `problems/` folders so you can see exactly what happened.

## Requirements

- Python 3.14+
- [`exiftool`](https://exiftool.org/) — the script refuses to start without it (`brew install exiftool` on a Mac)
- [`pixi`](https://pixi.sh) (recommended) — manages both of the above for you

## Install

```
pixi install
```

This creates a `pixi`-managed environment with Python and `exiftool` pinned, and installs the project's own dependencies.

If you'd rather not use `pixi`, any Python 3.14+ environment with `exiftool` on your `PATH` works — `python3 -m pip install .` then run `python3 main.py ...` directly.

## Before running

Download the *entire* Takeout export you want to process before running the script. Google Takeout splits large exports across multiple archives and doesn't guarantee that an image and its JSON sidecar land in the same archive — if you run the script archive-by-archive as they finish downloading, some files may be missing their sidecar and get treated as unfixable when they aren't.

Extract everything into one folder (subfolders/albums are fine — the layout is preserved) and point the script at that.

## Usage

**Always do a dry run first.** It reports exactly what would happen — including per-file EXIF/GPS status and a full summary — without renaming, moving, or writing to a single file:

```
pixi run run path/to/extracted-takeout --dry-run
```

For a huge export, preview a random subset instead of scanning everything (still requires `--dry-run`):

```
pixi run run path/to/extracted-takeout --dry-run --sample 5
```

Read the generated `timestamper_dryrun_report.md` inside that folder. Once you're happy, run for real:

```
pixi run run path/to/extracted-takeout
```

(Equivalently: `pixi run python main.py path/to/extracted-takeout [--dry-run] [--sample PERCENT]`.)

**Note:** the script expects the folder to contain only image/video files and their associated JSON sidecars (plus album subfolders). There's no filtering by file extension — anything that isn't `.json`, `.md`, or `.DS_Store` is treated as a photo/video and handed to `exiftool`.

### What happens to each file

For every image/video found:

1. If it already has a plausible EXIF timestamp and GPS position (i.e. not suspiciously recent, not null-island `0,0`), it's left alone — only its filesystem mtime is synced to match.
2. Otherwise, the script looks for the file's JSON sidecar and, if found, writes the real taken-time and location from the JSON into the file's EXIF tags (and the equivalent QuickTime tags for `.mp4`/`.mov`, since that's what Finder and Apple Photos actually read for a video's date/location) and syncs the filesystem mtime to match.
3. If no sidecar is found and the existing EXIF isn't usable either, the file is left as-is and flagged.

Every file then gets moved (real runs only — `--dry-run` moves nothing):

- **`ready/`** — successfully fixed or already-good files, in their original album subfolder structure
- **`sidecars/`** — the matched JSON sidecar for each `ready/` file
- **`problems/`** — anything with no usable timestamp/sidecar, or that errored out

A `timestamper_report.md` (or `timestamper_dryrun_report.md` for a dry run) is written summarizing every file's outcome. Emptied album folders are cleaned up afterwards.

There's currently no backup step — if you're not confident in the result, re-run `--dry-run` and check the report rather than the real thing, and keep the Takeout export around until you've verified the results in Photos.

## JSON sidecar naming

Google Takeout doesn't always name a sidecar exactly `Image.ext.json`. The script tries several strategies, in order, before giving up:

1. **Standard naming** — `Image.jpg.json`, or the newer `Image.jpg.supplemental-metadata.json` format. Takeout truncates sidecar filenames to 51 characters total, so very long filenames get a truncated-name variant tried too.
2. **Duplicate files** — if you have two images named `Image.jpeg`, the second is `Image(1).jpeg` on disk, but its sidecar is named `Image.jpeg(1).json` (the `(1)` moves after the extension).
3. **Edited images** — `Image-edited.jpg` (Google Photos' edited-copy suffix) has no sidecar of its own; its metadata lives in `Image.jpg.json`, the original's sidecar.
4. **Fuzzy fallback** — for older exports affected by a macOS quirk where the sidecar filename picked up an extra timestamp (e.g. `Image.jpg 1.09.32 PM.json`), the script falls back to matching on filename containment.

## Development

```
pixi run test       # fast unit tests
pixi run test-all   # everything, including integration tests that exercise real exiftool
```
