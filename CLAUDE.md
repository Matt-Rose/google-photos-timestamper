# google-photos-timestamper

Restores EXIF/QuickTime timestamp and GPS metadata to Google Takeout photo/video
exports from their JSON sidecars, then sorts everything into `ready/`/`sidecars/`/
`problems/`. See README.md for user-facing usage; this file is for whoever (human
or Claude) next touches the code.

## Status

Rewritten and hardened (2026-07-26/27) ahead of a real ~750GB family photo
migration. Full pytest suite in place (`pixi run test` / `pixi run test-all`).
Known correctness bugs from the original script have been fixed; see "Fixed
gotchas" below before assuming similar-looking code elsewhere is still broken.

## Architecture notes

- Flat `main.py` at the repo root (no `src/` package layout) — deliberate,
  this is a single-script personal tool, not worth restructuring. Tests import
  it via a root-level `conftest.py` that adds the repo root to `sys.path`.
- The CLI driver lives in `main()`, guarded by `if __name__ == "__main__":` —
  this is the *only* reason the module is import-safe for tests. Anything
  added to the bottom "Main" section must stay inside `main()`, not at module
  level, or tests/imports will start executing it.
- `process_file()` takes `dry_run: bool` and threads it through every
  mutating call (`os.utime`, `write_exif_tags`). Dry-run must never call
  either — it computes the same decision logic and returns the same
  `Outcome`/`notes`, just skips the actual writes.
- `FileResult.sidecar_data` carries the parsed JSON sidecar (when one was
  read), including on `Outcome.ERROR` results — this is what lets the
  dry-run report's sidecar-shape checks and field survey work even when
  parsing a sidecar throws. See `process_file()`'s `json_data = None` /
  `json_path = None` initialization above the `try:` block — needed so the
  outer `except` can always reference them safely.

## Fixed gotchas (don't reintroduce)

1. **Video files need QuickTime tags, not just EXIF/XMP.** `write_exif_tags()`
   writes `-DateTimeOriginal`/`-DateTimeDigitized` *and* the QuickTime
   `CreateDate`/`ModifyDate`/`Track*`/`Media*` tags, plus
   `QuickTime:GPSCoordinates`. Finder and Apple Photos read the QuickTime
   tags for `.mp4`/`.mov` files — EXIF/XMP tags alone are invisible to them.
   Writing the QuickTime tags on a photo file is a harmless no-op (confirmed:
   exiftool exits 0, tag just doesn't apply), so there's no need to branch on
   file type.

2. **Set mtime *after* the exiftool write, not before.**
   `exiftool -overwrite_original` rewrites the file on any write, which
   resets its mtime to wall-clock "now" — so if you set mtime first, it gets
   silently clobbered the moment `write_exif_tags()` runs. This bit for
   *any* write (GPS-only writes included), not just timestamp writes.

3. **Sidecar JSON shape isn't guaranteed.** `_describe_timestamp_anomaly()` /
   `_describe_geo_anomaly()` validate `photoTakenTime.timestamp` and
   `geoData`/`geoDataExif` lat/lon shape *before* the crash-prone direct-index
   extraction, raising a specific error instead of a bare `KeyError`. If you
   add a new sidecar field read, index defensively or add a matching
   anomaly-check — Takeout's JSON schema has had at least one real-world
   naming quirk already (`supplemental-metadata` suffix); assume there could
   be more we haven't seen yet.

## `--dry-run` design

- Never calls `os.utime()` or `write_exif_tags()` — by construction, it
  *cannot* predict an exiftool write failure (e.g. a genuinely corrupt file).
  It can only surface sidecar-matching problems and Python-level exceptions.
  This is a known, accepted limitation, not an oversight.
- Writes to a distinctly-named `timestamper_dryrun_report.md`
  (vs `timestamper_report.md` for a real run) so the two can never collide or
  be mistaken for each other.
- The dry-run report is **aggregated by pattern**, not one line per file —
  outcome-by-extension, grouped error messages with capped examples, and a
  sidecar-field survey. This was deliberate: at real-Takeout-library scale
  (tens of thousands of files), a per-file report is unreadable. The
  real-run report (`write_report`) is intentionally *not* aggregated, since
  it doubles as an audit trail of exactly what happened to each file.
- `--sample PERCENT` requires `--dry-run` — sampling a real run would leave
  the untouched majority in a confusing half-migrated state.

## Testing

- `pixi run test` — fast unit tests (`test/unit/`)
- `pixi run test-all` — everything, including integration tests that shell
  out to a real `exiftool` against tiny committed fixtures
  (`test/fixtures/sample.jpg`, `sample.mov`) — never mutate those fixtures
  directly, always copy to `tmp_path` first (see `conftest.py`).
- Integration tests are a deliberate choice here, not just unit tests with
  mocks: the bugs that mattered (QuickTime tags, mtime-ordering) only show up
  against a real exiftool invocation. A mocked subprocess call would have
  hidden both.

## Known limitations (accepted, not bugs to fix reflexively)

- No backup-before-running step. Considered and explicitly rejected due to
  disk space constraints on a real migration — `--dry-run` is the safety net
  instead.
- `piexif` was removed as a dependency (unused — the script shells out to
  `exiftool` exclusively, which is now a declared `pixi` dependency so
  `pixi install` alone is sufficient; no reliance on a manually-brewed copy).
