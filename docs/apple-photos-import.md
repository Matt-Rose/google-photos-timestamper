# Importing into Apple Photos — hard-won operational knowledge

This repo's `main.py` restores metadata to files. Getting those files *into*
Apple Photos is a separate problem with its own failure modes, all of which
cost days to diagnose. This document exists so they are not re-diagnosed.

Everything here was learned from two real migrations: ~284k Takeout files for
one library (2026-07 to 2026-09) and 5,842 shared-album files for another
(2026-09).

## The mechanism that breaks imports

**Photos hangs inside AppleScript event handling.** Not "runs slowly" — hangs.

Each album add opens a modal *"Adding photos to album"* progress sheet. When
one stalls, the modal blocks Photos' main run loop, and every subsequent Apple
Event queues behind it. Each retry stacks another modal. Observed 2026-09-06:
five stacked modals with frozen progress bars and no way to dismiss them.

Signature to look for:

    the importing process sits at 0.0% CPU for tens of minutes

A hang report (`Photos_2026-08-26-064608.hang`) shows the main thread in
`AEProcessAppleEvent -> dispatchRawAppleEvent -> NSCountCommand ->
runUntilDate -> mach_msg_trap`, unresponsive for 35,419 seconds with under
0.001s of CPU.

**A polite `quit` cannot recover this** — the quit is itself an Apple Event,
queued behind the blocked modals. Force-quit is the only way. Confirmed
2026-09-06: `activate` returned `-1712 AppleEvent timed out` while a trivial
`get name` still answered. **Partial responsiveness is not proof of health.**

**Consequence for any import harness**: relaunching Photos only when the
process is *absent* is not enough. A hung Photos is still present. The harness
must probe whether it *answers*, and force-restart it when it does not. An
import run that skips this will fire batch after batch at a dead app,
accomplishing nothing while still creating import-session records.

## Rules for a working import harness

Each rule below was learned by getting it wrong first.

1. **Probe with `count of albums`, never `count of media items`.**
   Counting ~44k assets takes about 40 seconds on a real library, so an
   expensive probe cannot distinguish slow from dead. Albums number in the
   hundreds and walk the same AppleScript path. Use a 90s timeout.

2. **Require two consecutive good probes, 20s apart, before trusting it.**
   One slow answer must not condemn a healthy app, and one lucky answer must
   not clear a sick one.

3. **Escalate when quitting**: polite quit (30s grace) -> `SIGTERM` (20s) ->
   `SIGKILL`. Expect the polite quit to fail whenever it actually matters.

4. **Relaunch with `open -a Photos`, NEVER `tell application "Photos" to
   launch`.** `launch` starts the app *without* an open event, which is how
   you get "running in the taskbar with no window". photoscript uses `launch`
   internally — this is why Photos comes back unusable after it kills the app.

5. **Wait ~90s after relaunch before importing.** Photos must index the
   library first. Importing too early produces
   `Error getting duplicate photo: Invalid photo id: <uuid>` for assets that
   demonstrably exist in the database.

6. **Wait for the analysis daemons to go quiet before each batch.** Block
   until summed `mediaanalysisd` + `photoanalysisd` + `cloudphotod` CPU is
   below ~25%, with a cap (30 min) so it cannot wait forever.

7. **Retry the whole batch with backoff**, not just the failing call.

8. **Suppress photoscript's `killall Photos`.** See below.

## photoscript kills Photos, and the patch must load via PYTHONPATH

`photoscript/script_loader.py` wires `kill_photos_app()` as tenacity's
`before_sleep` hook on `run_script`. *Any* AppleScript error containing
"timed out" therefore triggers `killall Photos`, a 5s wait, and one retry. A
second timeout re-raises and osxphotos exits 1.

Combined with rule 4, that is catastrophic: the kill lands, photoscript
relaunches via `launch`, and every later call fails against a windowless app.

`tools/sitecustomize.py` replaces the kill with a logging no-op, keeping the
retry. **It must be loaded via `PYTHONPATH`.** A copy placed in the venv's
`site-packages` never loads, because Homebrew ships its own `sitecustomize.py`
inside the stdlib directory, which precedes `site-packages` on `sys.path`.
The patch chains to Homebrew's copy first, since that does real path setup.

Measured effect (2026-09-06): Photos survived 2h47m across 8 AppleScript
timeouts that would previously have destroyed it 8 times.

## WAL bloat: `mediaanalysisd` is the pinned reader

Photos libraries here grew `Photos.sqlite-wal` to 26 GB and 59 GB while the
main database stayed frozen at its creation size.

`lsof` identifies the cause:

    mediaanalysisd  9r, 10r, 22r, 23r -> Photos.sqlite and Photos.sqlite-wal

SQLite can never reset a WAL while any reader holds a snapshot inside it, and
`mediaanalysisd` holds read descriptors continuously while working through an
analysis backlog. A fresh library means a huge backlog, so the WAL only grows.

The WAL header's checkpoint sequence number (bytes 12-15) reads **0** in this
state: never checkpointed since creation.

**Fixes**, in order of preference:

- Quit Photos and its daemons, then relaunch. Releasing the pinned reader lets
  the checkpoint complete on its own. This cleared 26.5 GB -> 4.9 MB.
- An explicit `PRAGMA wal_checkpoint(TRUNCATE);` with **every** connection
  closed. This cleared 59.7 GB -> 0 in 1537s. It requires the owning user to
  be logged out: `photolibraryd` is a LaunchAgent that respawns instantly, and
  a respawned reader blocks the truncation indefinitely.

**Do not diagnose WAL size as the cause of a slow import without checking.**
A library with a 49 GB WAL imported ~3.4k items successfully while one with a
26.5 GB WAL failed. The WAL is a real problem for disk space and startup time;
it was not the discriminator for import success.

## Reading a Photos library safely

    sqlite3 "file://<lib>/database/Photos.sqlite?mode=ro" "select count(*) from ZASSET;"

**Use `mode=ro`, never `immutable=1`.** `immutable=1` ignores the `-wal` file
and reports a snapshot frozen at the last checkpoint. Measured divergence:

    immutable=1 ->  1,100 assets        mode=ro -> 46,629
    immutable=1 ->  2,203 assets        mode=ro -> 25,014

A monitoring check built on `immutable=1` silently reports weeks-old numbers.

Useful queries:

    -- real user albums only; ZGENERICALBUM is dominated by import sessions
    select count(*) from ZGENERICALBUM where ZKIND=2;
    -- import sessions (kind 1510) — one per import group, they accumulate fast
    select count(*) from ZGENERICALBUM where ZKIND=1510;
    -- trashed assets still matchable by --skip-dups
    select count(*) from ZASSET where ZTRASHEDSTATE!=0;

**Never compare raw `ZGENERICALBUM` counts between libraries.** They are
dominated by kind-1510 import sessions. Compare `ZKIND=2`.

### iCloud Shared Albums live in `ZSHARE`, not `ZGENERICALBUM`

    -- the shared albums themselves (ZSCOPETYPE 0 = shared album, 4 = Shared Library)
    select Z_PK, ZSCOPETYPE, ZTITLE, ZASSETCOUNT from ZSHARE;
    -- their members: ZASSET.ZCOLLECTIONSHARE joins to ZSHARE.Z_PK
    select ZCOLLECTIONSHARE, count(*) from ZASSET where ZCOLLECTIONSHARE is not null group by 1;

This makes shared-album membership fully readable, which is the only way to
compute what a shared album is actually missing before adding to it.

## Permissions

- **Automation**: `osxphotos import` drives Photos over AppleScript and fails
  with `Not authorised to send Apple events to Photos. (-1743)` until the
  *hosting terminal app* is granted Automation control of Photos. No prompt
  appears once a denial is recorded — grant it by hand in System Settings >
  Privacy & Security > Automation, or `tccutil reset AppleEvents <bundle-id>`
  to make it re-prompt. **Grants are per-user**: granting it in one account
  does nothing for another.

- **Grant Full Disk Access to the terminal app, and be done with it.** The
  narrower "removable volumes" consent *lapsed three times in two days* on a
  live machine, each time silently, mid-task. Full Disk Access on the hosting
  terminal (a signed Apple app that already needs Automation for Photos) is
  stable and covers the library volume. Chasing the narrow grant cost hours.

- **A TCC denial on the library volume looks exactly like a hung Photos.**
  `osxphotos` copies `Photos.sqlite` to a temp directory for duplicate
  detection, so when the volume is blocked *every* chunk fails instantly and
  identically:

      OSError: Error Domain=NSCocoaErrorDomain Code=513 "Photos.sqlite"
      couldn't be copied because you don't have permission to access ...
      NSUnderlyingError=... "Operation not permitted"

  Without that message the symptom is just "every chunk failed", which reads
  as a hang. It is not: Photos answers probes normally throughout. **Check
  whether Photos answers before concluding it is hung**, and never discard
  the subprocess output — the same command went 0/4 blocked and 4/4 once
  Full Disk Access was granted.

- **SSH is a file-access workaround only, not a Photos one.** With Full Disk
  Access on `/usr/libexec/sshd-keygen-wrapper`, `ssh localhost` reads the
  library volume even when the interactive session cannot. But an SSH session
  lands in launchd's `Background` namespace with no WindowServer, so it
  cannot drive Photos over AppleScript. An import needs file access *and*
  Apple Events at once, so SSH alone can never run one.

- **Full Disk Access / removable volumes**: file access failures on an
  external volume are TCC, not Unix permissions. TCC attributes the access to
  the *responsible process*, which is the GUI app that launched the chain —
  not the shell, not tmux. A CLI tool launched from a detached tmux server has
  no GUI ancestor and lands in launchd's `Background` namespace, where TCC
  cannot display a consent prompt and therefore silently denies. Check with
  `launchctl managername` (`Aqua` vs `Background`).

  Both TCC databases are readable with `immutable=1` even while file access is
  blocked, which is the fastest way to see who is actually denied:

      sqlite3 "file:$HOME/Library/Application Support/com.apple.TCC/TCC.db?immutable=1" \
        "select service, client, auth_value from access;"

  `auth_value`: 0 = denied, 2 = allowed.

- **Spotlight works when file reads do not.** `mdfind -onlyin <vol> -attr
  kMDItemFSName -attr kMDItemContentCreationDate -attr kMDItemPixelWidth
  "kMDItemFSName == '*.jpg'"` returns names and indexed metadata without
  opening files. Caveats: `kMDItemFSName == '*'` matches nothing (use `'*.*'`),
  and `kMDItemContentCreationDate` silently falls back to the filesystem date
  for files with no EXIF — treat it as a candidate, not ground truth.

## Import flags that matter

    osxphotos import <files> --album <name> \
        --skip-dups --dup-albums --auto-live --resume --report <csv> --verbose

- `--skip-dups --dup-albums` — the mechanism the whole album-rebuild approach
  depends on: when a file is already in the library, the **existing** asset is
  added to the album instead of being re-imported. Plain drag-and-drop cannot
  do this; answering "Don't Import" to Photos' duplicate prompt leaves the
  existing asset *out* of the album.
- `--auto-live` — pairs a still and video that lack a `ContentIdentifier`.
- `--resume` — skips files already recorded in
  `~/.local/share/osxphotos/osxphotos_import.db`. Note it only records files
  genuinely *imported*; assets added to albums via `--dup-albums` are not
  recorded, so they are re-processed on a re-run (harmless).
- Duplicate matching uses **fingerprint** for photos but **lowercase filename
  + size** for videos, so a same-named, same-sized *different* video can false
  match.

**Purge Recently Deleted before importing.** Trashed items remain in the
database for 30 days and can still match `--skip-dups`, in which case
`--dup-albums` adds a *deleted* asset to the album.

## Verifying an import: filenames lie

Do not verify coverage by comparing filenames. A library accumulates the same
photo under several naming conventions, and a naive comparison reports files
as missing that are present. Checking one album four times with successively
better rules gave "missing" counts of 37, then 2, then 1, then 0 — none were
ever actually absent:

    1,043  exact filename match
       21  stem truncated to 47 chars (Takeout's truncation)
       14  "(N)" duplicate suffix — on disk OR in the library, both directions
        1  "_Original.JPG" suffix

`--skip-dups` matches on **fingerprint**, so it finds all of these correctly;
it is the filename check that is wrong. When verifying, either apply every
rule above or compare counts rather than names:

    distinct filenames in album  ==  filename-group count on disk

That equality held exactly for every album once the import was complete, and
is a far more reliable check than set differences on names.

Note also that `ZCACHEDCOUNT` on `ZGENERICALBUM` lags. Count the join table
(`Z_<n>ASSETS`) for a true membership figure.

## Things that are not the problem

Recorded because each cost real time:

- Album count. One library had 344 real albums, the other 151; the one with
  fewer albums was the one that failed.
- Daemon contention. Measured at under 2% CPU across all photo daemons during
  a failing import.
- Shared-library participation. Identical on both libraries (5,288 assets).
- WAL size. See above — the bigger WAL succeeded.
