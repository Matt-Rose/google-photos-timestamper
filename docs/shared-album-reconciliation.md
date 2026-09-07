# Rebuilding Google Photos shared albums in Apple Photos

Google Takeout exports only albums *you* created, and within those only the
photos *you personally* added. A shared album's contributions from other
people are missing from everybody's Takeout. This document records the method
that works, and the measurements behind it.

## Get the album contents from the web UI, not Takeout

The Google Photos Library API cannot help: since **2025-04-01**,
`mediaItems.list`/`search` only return items created by the calling app, so no
script can enumerate an existing shared album. The Picker API is an
interactive per-session picker, not bulk listing.

Use **"Download all"** on the album in the Google Photos web UI.

**Crucially, this returns the original file bytes with EXIF intact** — the
opposite of Takeout, which strips timestamps into `.json` sidecars (the thing
this repo's `main.py` exists to undo). Measured over 5 albums, 5,842 files:

    5,539 (94.8%)  already had a usable capture date
      303          did not

So do not build a sidecar-matching pipeline for the bulk. The download is
authoritative; matching is only needed for the dateless remainder.

The dateless files are genuinely dateless at source, not download damage:
Picasa-era UUID-named uploads, WhatsApp forwards (`IMG-20180225-WA0014.jpg`),
screenshots and collages.

## Recovering the missing dates

Three passes, cheapest first. Over 303 files this reached 293 (96.7%).

1. **Filename.** Patterns like `PXL_20230531_145342087`,
   `IMG-20180225-WA0014`, `00030IMG_00030_BURST20190804121948` encode the
   timestamp directly.

2. **Filename + aspect ratio**, matched against an existing Takeout tree.
   Match on aspect ratio (tolerance 0.01), **not** exact dimensions — the
   download may be a downscale of the same image. Reject on aspect mismatch;
   that correctly rejected crops. Require all duplicate copies to agree on the
   date. 255 of 300 resolved this way.

   Independent corroboration: 176 of those sat inside a `Photos from YYYY`
   folder, which encodes a year Google assigned separately. **176 of 176
   agreed** — `main.py`'s `_photos_from_year()` doubles as a free validator.

3. **Pixel comparison** for what remains. Decode both images to a 64x64
   contrast-normalised greyscale signature and compare RMS distance. This
   resolves counter collisions that filename matching cannot: `IMG_4853.JPG`
   had candidates dated 2016, 2017, 2018 and 2025; pixels picked 2025
   decisively. All 35 matches came out **pixel-identical** (RMS < 0.01).

   **Accept when close candidates agree on the date; do not require the best
   score to beat the runner-up.** Takeout duplicates a file into both a year
   folder and an album folder, so near-identical runners-up are expected. A
   uniqueness test wrongly discarded a 0.000 match because a 0.007 duplicate
   existed.

## Filename matching is far weaker than it looks

Joining 5,842 filenames against a 284k-file Takeout tree appeared to give
97.4% coverage. It does not survive scrutiny:

    name class                        n     hit    rate   multi-hit
    generic counter (IMG_1234.JPG) 4024    4024  100.0%        3930
    timestamped (PXL_/IMG_ + dt)    941     819   87.0%         128
    UUID                            228     227   99.6%          34
    burst                           100      98   98.0%          25
    WhatsApp dated                   11      10   90.9%           0

Every generic-counter name "matches", and 98% of those match *several* paths —
the signature of a meaningless join. Only names unique by construction
(UUID, timestamped, burst) are trustworthy: 1,154 of 1,280, 90.2%.

## Live Photos survive the round trip

Google splits a Live Photo into a still and a video, but **the web download
preserves Apple's `ContentIdentifier`**. Over 992 same-stem pairs:

    936  identifier present on BOTH halves AND matching
     14  only the still had it   -> writable into the video
     30  only the video had it   -> NOT fixable, see below
     12  neither

Writing it into a **video** works:

    exiftool -overwrite_original -Keys:ContentIdentifier=<uuid> file.mov

Re-assert the capture date afterwards; exiftool rewrites the file.

Writing it into a **HEIC still does not work**. Both
`-MakerNotes:ContentIdentifier=` and plain `-ContentIdentifier=` fail silently
with `0 image files updated`, because the tag lives in Apple MakerNotes and
exiftool will not create that structure. Use `osxphotos import --auto-live`,
which pairs identifier-less still/video pairs on import.

**No renaming is needed.** `.heic` + `.mp4` pairs are grouped correctly;
osxphotos reports "Processing live photo pair" and the result has
`live_photo=True` with a populated `path_live_photo`. Apple's `.MOV`
convention is not required.

## Video codecs

Google returns some videos as **VP9**, which Photos refuses silently — they
import as nothing at all, with no error.

    1,232 videos:  973 hvc1 · 193 avc1 · 66 vp09

Transcode the VP9 ones, but **check for HDR first**:

    19 files  yuv420p     bt709                    SDR
    47 files  yuv420p10le bt2020nc / arib-std-b67  HLG HDR, 10-bit

A naive H.264 encode yields 8-bit output *still tagged as HDR* — banding plus
wrong tone-mapping. Correct handling:

    # HDR
    ffmpeg -i in -c:v libx265 -crf 20 -preset medium -pix_fmt yuv420p10le \
           -tag:v hvc1 -c:a copy -map_metadata 0 -movflags +faststart out
    # SDR
    ffmpeg -i in -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p \
           -c:a copy -map_metadata 0 -movflags +faststart out

`libx265` also beat VideoToolbox on size (16.7 MB vs 31.4 MB for an 18.2 MB
source), at ~30s per clip versus ~6s.

Watch for VP9 inside **`.MOV` containers** — the extension does not tell you
the codec. And re-assert capture dates from a list captured *after* any date
fixes, or a stale entry silently skips the re-assertion.

## Apple's two "shared" features are not interchangeable

- **iCloud Shared Album** — both parties see one album, either can add. But
  photos are downscaled to 2048px and videos to 720p, 5,000 items per album.
  **Cannot be created by AppleScript or osxphotos — UI only.**
- **iCloud Shared Photo Library** — full-resolution originals, up to 6
  participants, counts against the owner's storage. But **albums are not
  shared between participants**; only assets are.

For an archive, the answer is usually both: originals in the Shared Photo
Library, Shared Albums on top as the browsing surface. Note that every item
will show the person who added it as the contributor — original per-contributor
attribution from Google is not recoverable.

## Live Photos only pair on first import

A still that is **already in the library** cannot be retrofitted into a Live
Photo. osxphotos imports photo *groups*; if the group's still is a duplicate,
`--skip-dups` skips the whole group, `--dup-albums` adds the existing still to
the album, and the video is never imported at all.

Measured on one album: 45 still+video pairs, but only ~7 stills were new, and
exactly 8 became Live Photos. Every pair that *could* pair, did.

This matters most when the target library was seeded by **dragging Takeout
folders into the Photos GUI**, which imports stills and videos as separate
assets with no pairing. Such a library will contain thousands of unpaired
Live Photo halves, and importing the same photos again will not fix them.

Fixing it would mean deleting assets that may have years of history — edits,
album memberships, iCloud identity — to regain a second of motion. Usually
the wrong trade. Decide deliberately rather than by default.

## Files that will not resolve

Expect a small tail that no method reaches: crops (the download is a cropped
edit, so no pixel match exists), counter collisions where the original is
gone, and third-party contributions that exist in nobody's Takeout. Over 5,842
files this was 10 files, 0.17%. Import them undated rather than blocking on
them.
