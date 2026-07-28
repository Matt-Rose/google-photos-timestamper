import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────────

# Timestamps within this many days of today are assumed to be download/import
# dates, not genuine photo-taken dates.
RECENT_DAYS = 5

# Subdirectory names created by this script — skipped during the input walk.
OUTPUT_DIRS = {"ready", "sidecars", "problems"}


# ── Data structures ────────────────────────────────────────────────────────────


class Outcome(Enum):
    UPDATED = "updated"  # mtime and/or EXIF written from JSON
    GOOD_EXIF = "good_exif"  # existing EXIF was fine; mtime synced from it
    NO_JSON = "no_json"  # no sidecar found and no reliable EXIF
    ERROR = "error"  # unexpected exception


@dataclass
class FileResult:
    path: str
    outcome: Outcome
    notes: str = ""
    json_path: Optional[str] = None  # resolved sidecar path, if one was used
    error: Optional[str] = None
    sidecar_data: Optional[dict] = None  # parsed JSON sidecar, if one was read
    assigned_timestamp: Optional[float] = None  # epoch mtime this file was/would be given


# ── Exceptions ─────────────────────────────────────────────────────────────────


class NoJsonFileFoundError(Exception):
    pass


# ── JSON sidecar finding (strategy chain from original script) ─────────────────

stem_regex = r".*\(\d+\)\..*"


def get_alike_regex(filename: str) -> str:
    tokens = filename.split(".")
    name = re.escape(".".join(tokens[:-1]))
    ext = re.escape(tokens[-1])
    return rf".*{name}( (\d{{1,2}}\.){{2}}\d{{1,2}} PM)+\.{ext}\..*"


def get_alike_regex_with_duplication(filename: str) -> str:
    return rf".*{re.escape(filename)}( (\d{{1,2}}\.){{2}}\d{{1,2}} PM)+\..*"


def move_duplication_string(path: str) -> str:
    match = re.search(r"(.*)\((.*?)\)(\..*)", path)
    if match:
        return match.group(1) + match.group(3) + "(" + match.group(2) + ")"
    return path


def get_alike_json(path: str) -> Optional[str]:
    dir_path = os.path.dirname(path)
    file_name = os.path.basename(path)
    jsons = [
        os.path.join(dir_path, f) for f in os.listdir(dir_path) if f.endswith(".json")
    ]

    regex = get_alike_regex(file_name)
    for j in jsons:
        if re.match(regex, j):
            return j

    moved_name = os.path.basename(move_duplication_string(path))
    regex = get_alike_regex_with_duplication(moved_name)
    for j in jsons:
        if re.match(regex, j):
            return j

    return None


# Google Takeout truncates sidecar filenames so the total length is ≤ 51 chars
# (46 chars of stem + ".json").  The stem is built as:
#   "{image_filename}.supplemental-metadata"   (new format)
#   "{image_stem}.supplemental-metadata"       (new format, no extension)
#   "{image_filename}"                          (old format)
#   "{image_stem}"                              (old format, no extension)
# When the stem exceeds 46 characters it is truncated to exactly 46 before
# appending ".json".
_MAX_SIDECAR_STEM = 46  # chars before ".json"


def _sidecar_candidates(filename: str, dir_path: str) -> list:
    """Return candidate sidecar paths for a given image filename."""
    stem = os.path.splitext(filename)[0]
    candidates = []
    for name_form in [filename, stem]:
        for suffix in [".supplemental-metadata.json", ".json"]:
            base = name_form + suffix[:-5]  # everything before ".json"
            full = base + ".json"
            candidates.append(os.path.join(dir_path, full))
            if len(full) > _MAX_SIDECAR_STEM + 5:
                candidates.append(
                    os.path.join(dir_path, base[:_MAX_SIDECAR_STEM] + ".json")
                )
    return candidates


def get_json_path_and_data(image_path: str) -> Tuple[dict, str]:
    """Find and load the JSON sidecar for image_path.

    Returns (json_data, resolved_json_path).
    Raises NoJsonFileFoundError if nothing is found.
    """
    dir_path = os.path.dirname(image_path)
    filename = os.path.basename(image_path)

    candidate_paths: list = []

    # Primary filename candidates
    candidate_paths.extend(_sidecar_candidates(filename, dir_path))

    # Duplication-string-moved variant  e.g. "IMG(1).jpg" → "IMG.jpg(1)"
    moved = os.path.basename(move_duplication_string(image_path))
    if moved != filename:
        candidate_paths.extend(_sidecar_candidates(moved, dir_path))

    # Strip "-edited" suffix
    no_edited = filename.replace("-edited", "")
    if no_edited != filename:
        candidate_paths.extend(_sidecar_candidates(no_edited, dir_path))

    # Fuzzy fallback for macOS-timestamp variants (legacy format)
    alike = get_alike_json(image_path)
    if alike:
        candidate_paths.append(alike)

    seen: set = set()
    for p in candidate_paths:
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(p, "r") as f:
                data = json.load(f)
            return data, p
        except (FileNotFoundError, OSError):
            continue

    sibling = _find_live_photo_sibling_json(image_path)
    if sibling is not None:
        return sibling

    raise NoJsonFileFoundError(f"Could not find JSON sidecar for {image_path}")


# Video half of a Live Photo (iPhone) / Motion Photo (Android) pair -- Google
# Takeout gives the still-image half a sidecar but not the video half.
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mp", ".3gp", ".avi"}
_LIVE_PHOTO_SIBLING_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png"}


def _find_live_photo_sibling_json(image_path: str) -> Optional[Tuple[dict, str]]:
    """For a video with no sidecar of its own, borrow its paired still
    image's sidecar -- they were captured at the same instant, so the same
    timestamp/GPS applies to both.

    Matches by filename prefix: iPhone Live Photo pairs share a stem
    (`IMG_6375.HEIC` / `IMG_6375.MP4`); Android Motion Photo pairs have the
    video's full filename as a prefix of the photo's filename
    (`X.MP` / `X.MP.jpg`). Both are covered by "sibling starts with the
    video's stem, followed immediately by a `.`" -- which also rules out
    unrelated files that merely share a numeric prefix (e.g. `IMG_100.MP4`
    must not match `IMG_1000.HEIC`).
    """
    dir_path = os.path.dirname(image_path)
    filename = os.path.basename(image_path)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _VIDEO_EXTENSIONS:
        return None

    stem = os.path.splitext(filename)[0]
    try:
        siblings = os.listdir(dir_path)
    except OSError:
        return None

    for sibling in siblings:
        if sibling == filename:
            continue
        if not sibling.startswith(stem) or not sibling[len(stem):].startswith("."):
            continue
        if os.path.splitext(sibling)[1].lower() not in _LIVE_PHOTO_SIBLING_EXTENSIONS:
            continue
        try:
            return get_json_path_and_data(os.path.join(dir_path, sibling))
        except NoJsonFileFoundError:
            continue

    return None


# ── EXIF helpers ───────────────────────────────────────────────────────────────


def read_exif_tags(path: str) -> dict:
    """Return selected EXIF tags as a dict via exiftool.

    Uses -n so GPS values come back as decimal numbers.
    Returns an empty dict on any failure.
    """
    try:
        result = subprocess.run(
            [
                "exiftool",
                "-json",
                "-n",
                "-DateTimeOriginal",
                "-GPSLatitude",
                "-GPSLatitudeRef",
                "-GPSLongitude",
                "-GPSLongitudeRef",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if data:
                return data[0]
    except Exception:
        pass
    return {}


def parse_exif_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def get_signed_gps(tags: dict) -> Tuple[Optional[float], Optional[float]]:
    """Return (lat, lon) as signed decimals, applying N/S/E/W refs."""
    raw_lat = tags.get("GPSLatitude")
    raw_lon = tags.get("GPSLongitude")
    if raw_lat is None or raw_lon is None:
        return None, None
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (TypeError, ValueError):
        return None, None
    if tags.get("GPSLatitudeRef", "N") == "S":
        lat = -lat
    if tags.get("GPSLongitudeRef", "E") == "W":
        lon = -lon
    return lat, lon


def timestamp_looks_bad(dt: datetime) -> bool:
    age_days = (datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)).days
    return age_days < RECENT_DAYS


def gps_looks_bad(lat: float, lon: float) -> bool:
    return abs(lat) < 0.001 and abs(lon) < 0.001


def get_geo_from_json(
    json_data: dict,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (lat, lon, alt) from a Takeout JSON, or (None, None, None).

    Prefers geoDataExif (original camera GPS) over geoData (Google's record).
    Rejects coordinates at null-island (0, 0).
    """
    for key in ("geoDataExif", "geoData"):
        geo = json_data.get(key) or {}
        lat = geo.get("latitude")
        lon = geo.get("longitude")
        alt = geo.get("altitude")
        if lat is not None and lon is not None and not gps_looks_bad(lat, lon):
            return float(lat), float(lon), float(alt) if alt is not None else None
    return None, None, None


def write_exif_tags(
    path: str,
    timestamp: Optional[float],  # None → don't write timestamp tags
    lat: Optional[float],
    lon: Optional[float],
    alt: Optional[float],
) -> None:
    """Write DateTimeOriginal/Digitized and/or GPS tags via exiftool.

    Also writes the equivalent QuickTime tags (CreateDate/ModifyDate/
    Track*/Media* dates, GPSCoordinates), since that's what Finder and Apple
    Photos actually read for a video's date/location -- EXIF/XMP tags alone
    are ignored for .mp4/.mov files. Writing them on a photo file is a
    harmless no-op (exiftool exits 0; the tag simply isn't applicable).

    Pass timestamp=None to write GPS only (preserves an existing good timestamp).
    -overwrite_original suppresses the creation of *_original backup files.
    On failure, retries once with -m (ignore minor errors) to handle files where
    the extension doesn't match the content (e.g. a JPEG named .HEIC).
    Raises subprocess.CalledProcessError if both attempts fail.
    """
    args = ["exiftool", "-overwrite_original"]

    if timestamp is not None:
        dt_str = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y:%m:%d %H:%M:%S"
        )
        args += [f"-DateTimeOriginal={dt_str}", f"-DateTimeDigitized={dt_str}"]
        for tag in (
            "QuickTime:CreateDate",
            "QuickTime:ModifyDate",
            "QuickTime:TrackCreateDate",
            "QuickTime:TrackModifyDate",
            "QuickTime:MediaCreateDate",
            "QuickTime:MediaModifyDate",
        ):
            args.append(f"-{tag}={dt_str}")

    if lat is not None and lon is not None and not gps_looks_bad(lat, lon):
        args += [
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
            f"-GPSLongitude={abs(lon)}",
            f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
        ]
        if alt is not None:
            ref = "Above Sea Level" if alt >= 0 else "Below Sea Level"
            args += [f"-GPSAltitude={abs(alt)}", f"-GPSAltitudeRef={ref}"]
        qt_coords = f"{lat}, {lon}" + (f", {alt}" if alt is not None else "")
        args.append(f"-QuickTime:GPSCoordinates={qt_coords}")

    args.append(path)

    result = subprocess.run(args, capture_output=True, timeout=30)
    if result.returncode != 0:
        # Retry with -m to suppress minor format-mismatch errors (e.g. a JPEG
        # with a .HEIC extension that exiftool refuses to process normally).
        retry = args[:1] + ["-m"] + args[1:]
        subprocess.run(retry, check=True, capture_output=True, timeout=30)


# ── Per-file processing ────────────────────────────────────────────────────────


def process_file(path: str, dry_run: bool = False) -> FileResult:
    """Process a single file.

    dry_run=True computes and reports what would happen but never calls
    os.utime() or write_exif_tags(), so no file is modified.
    """
    json_data = None
    json_path = None
    try:
        exif = read_exif_tags(path)
        exif_dt = parse_exif_datetime(exif.get("DateTimeOriginal"))
        exif_lat, exif_lon = get_signed_gps(exif)

        ts_good = exif_dt is not None and not timestamp_looks_bad(exif_dt)
        gps_good = (
            exif_lat is not None
            and exif_lon is not None
            and not gps_looks_bad(exif_lat, exif_lon)
        )

        # If EXIF already looks good, sync mtime and move on.
        if ts_good and gps_good:
            assert exif_dt is not None  # guaranteed by ts_good
            if not dry_run:
                os.utime(path, (exif_dt.timestamp(), exif_dt.timestamp()))
            # Still locate the sidecar so organise_files can move it to sidecars/.
            try:
                json_data, json_path = get_json_path_and_data(path)
            except NoJsonFileFoundError:
                pass
            return FileResult(
                path,
                Outcome.GOOD_EXIF,
                "mtime synced from existing EXIF",
                json_path,
                sidecar_data=json_data,
                assigned_timestamp=exif_dt.timestamp(),
            )

        # Try to find the JSON sidecar.
        try:
            json_data, json_path = get_json_path_and_data(path)
        except NoJsonFileFoundError:
            if ts_good:
                # Good timestamp but no GPS — still usable; sync mtime.
                assert exif_dt is not None  # guaranteed by ts_good
                if not dry_run:
                    os.utime(path, (exif_dt.timestamp(), exif_dt.timestamp()))
                return FileResult(
                    path,
                    Outcome.GOOD_EXIF,
                    "good EXIF timestamp (no GPS in sidecar); mtime synced",
                    assigned_timestamp=exif_dt.timestamp(),
                )
            return FileResult(path, Outcome.NO_JSON)

        ts_anomaly = _describe_timestamp_anomaly(json_data)
        if ts_anomaly:
            raise ValueError(f"sidecar timestamp shape unexpected: {ts_anomaly}")

        json_ts = float(json_data["photoTakenTime"]["timestamp"])

        # Pull GPS from JSON if EXIF GPS is missing/bad.
        if gps_good:
            lat, lon, alt = exif_lat, exif_lon, None
        else:
            lat, lon, alt = get_geo_from_json(json_data)

        # mtime: prefer the EXIF timestamp when it's already good, so we don't
        # clobber a precise camera clock with a potentially less accurate JSON value.
        if ts_good:
            assert exif_dt is not None  # guaranteed by ts_good
            mtime_ts = exif_dt.timestamp()
        else:
            mtime_ts = json_ts
        note_parts = ["mtime"]

        # Only write what actually needs updating — skip timestamp if already good,
        # skip GPS if already good or no usable GPS found in JSON.
        write_ts  = not ts_good
        write_gps = not gps_good and lat is not None

        if write_ts or write_gps:
            if dry_run:
                if write_ts:
                    note_parts.append("EXIF timestamp")
                if write_gps:
                    note_parts.append("EXIF GPS")
            else:
                try:
                    write_exif_tags(
                        path,
                        json_ts if write_ts else None,
                        lat   if write_gps else None,
                        lon   if write_gps else None,
                        alt   if write_gps else None,
                    )
                    if write_ts:
                        note_parts.append("EXIF timestamp")
                    if write_gps:
                        note_parts.append("EXIF GPS")
                except subprocess.CalledProcessError as e:
                    stderr = (e.stderr or b"").decode().strip().splitlines()
                    detail = stderr[-1] if stderr else "unknown error"
                    note_parts.append(f"EXIF write failed ({detail})")
                except subprocess.TimeoutExpired:
                    note_parts.append("EXIF write timed out")

        # Set mtime last: exiftool -overwrite_original rewrites the file on
        # any write, resetting its mtime to "now" -- so this must run after
        # write_exif_tags, not before, or the mtime ends up wrong.
        if not dry_run:
            os.utime(path, (mtime_ts, mtime_ts))

        return FileResult(
            path,
            Outcome.UPDATED,
            ", ".join(note_parts),
            json_path,
            sidecar_data=json_data,
            assigned_timestamp=mtime_ts,
        )

    except Exception as e:
        return FileResult(
            path,
            Outcome.ERROR,
            error=str(e),
            json_path=json_path,
            sidecar_data=json_data,
        )


# ── File organisation ──────────────────────────────────────────────────────────


def move_file(src: str, dest_root: str, base_dir: Optional[str] = None) -> None:
    """Move src into dest_root.

    If base_dir is given, preserve the relative path of src within dest_root
    (e.g. src=input/Album/a.jpg, base_dir=input → dest=dest_root/Album/a.jpg).
    The 1-to-1 mirror means collisions cannot occur in that case.

    Without base_dir, places the file flat in dest_root with collision avoidance.
    """
    if base_dir:
        dest = os.path.join(dest_root, os.path.relpath(src, base_dir))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        return

    os.makedirs(dest_root, exist_ok=True)
    name = os.path.basename(src)
    dest = os.path.join(dest_root, name)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(os.path.join(dest_root, f"{stem}_{i}{ext}")):
            i += 1
        dest = os.path.join(dest_root, f"{stem}_{i}{ext}")
    shutil.move(src, dest)


def _remove_empty_dirs(input_dir: str) -> None:
    """Walk bottom-up and remove album directories that are now empty.

    Skips the output dirs themselves and input_dir.
    Ignores .DS_Store when deciding whether a directory is empty.
    """
    for dirpath, *_ in os.walk(input_dir, topdown=False):
        if dirpath == input_dir:
            continue
        if os.path.basename(dirpath) in OUTPUT_DIRS:
            continue
        real_files = [f for f in os.listdir(dirpath) if f != ".DS_Store"]
        if not real_files:
            ds = os.path.join(dirpath, ".DS_Store")
            if os.path.exists(ds):
                os.remove(ds)
            try:
                os.rmdir(dirpath)
                print(f"  Removed empty dir: {os.path.relpath(dirpath, input_dir)}")
            except OSError as e:
                print(f"  Could not remove {dirpath}: {e}")


def organise_files(
    input_dir: str,
    results: list,
    report_path: str,
) -> None:
    processed_dir = os.path.join(input_dir, "ready")
    sidecars_dir = os.path.join(input_dir, "sidecars")
    problems_dir = os.path.join(input_dir, "problems")

    good_json_paths = {
        r.json_path
        for r in results
        if r.outcome in (Outcome.UPDATED, Outcome.GOOD_EXIF) and r.json_path
    }

    # Move image/video files, preserving album subdirectory hierarchy.
    for result in results:
        if not os.path.exists(result.path):
            continue
        if result.outcome in (Outcome.UPDATED, Outcome.GOOD_EXIF):
            move_file(result.path, processed_dir, base_dir=input_dir)
        else:
            move_file(result.path, problems_dir, base_dir=input_dir)

    # Move matched JSON sidecars.
    for json_path in good_json_paths:
        if os.path.exists(json_path):
            move_file(json_path, sidecars_dir, base_dir=input_dir)

    # Move any remaining files (orphan JSONs, unmatched sidecars, etc.) to problems/.
    for dirpath, dirnames, filenames in os.walk(input_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in OUTPUT_DIRS]
        for filename in filenames:
            fp = os.path.join(dirpath, filename)
            if fp == report_path or filename == ".DS_Store":
                continue
            move_file(fp, problems_dir, base_dir=input_dir)

    # Remove album directories that are now empty.
    _remove_empty_dirs(input_dir)


# ── Markdown report ────────────────────────────────────────────────────────────


def write_report(results: list, report_path: str, input_dir: str) -> None:
    updated = [r for r in results if r.outcome == Outcome.UPDATED]
    good_exif = [r for r in results if r.outcome == Outcome.GOOD_EXIF]
    no_json = [r for r in results if r.outcome == Outcome.NO_JSON]
    errors = [r for r in results if r.outcome == Outcome.ERROR]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Google Photos Timestamper Report",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        "| Outcome | Count |",
        "|---------|-------|",
        f"| Updated (EXIF/mtime written from JSON) | {len(updated)} |",
        f"| Good EXIF (not changed) | {len(good_exif)} |",
        f"| No JSON sidecar found | {len(no_json)} |",
        f"| Errors | {len(errors)} |",
        f"| **Total** | **{len(results)}** |",
        "",
    ]

    def rel(r) -> str:  # noqa: E306
        return os.path.relpath(r.path, input_dir)

    if updated:
        lines += ["## Updated", "", "| File | Changes |", "|------|---------|"]
        for r in sorted(updated, key=lambda r: r.path):
            lines.append(f"| `{rel(r)}` | {r.notes} |")
        lines.append("")

    if good_exif:
        lines += ["## Good EXIF (skipped)", ""]
        for r in sorted(good_exif, key=lambda r: r.path):
            lines.append(f"- `{rel(r)}` — {r.notes}")
        lines.append("")

    if no_json:
        lines += ["## No JSON Sidecar Found", ""]
        for r in sorted(no_json, key=lambda r: r.path):
            lines.append(f"- `{rel(r)}`")
        lines.append("")

    if errors:
        lines += ["## Errors", "", "| File | Error |", "|------|-------|"]
        for r in sorted(errors, key=lambda r: r.path):
            err = (
                (r.error or r.notes or "unknown error")
                .replace("|", "\\|")
                .replace("\n", " ")
            )
            lines.append(f"| `{rel(r)}` | {err} |")
        lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Report written → {report_path}")


# ── Dry-run summary report ──────────────────────────────────────────────────────


def _file_ext(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return ext if ext else "(no extension)"


def _is_meaningful(value) -> bool:
    """Heuristic for whether a sidecar field value carries real information."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return True


# Sidecar keys this script already reads and acts on.
_HANDLED_SIDECAR_KEYS = {"photoTakenTime", "geoData", "geoDataExif"}


def _describe_timestamp_anomaly(data: dict) -> Optional[str]:
    """None if data["photoTakenTime"]["timestamp"] is shaped as expected,
    otherwise a description of how it deviates."""
    ptt = data.get("photoTakenTime")
    if ptt is None:
        keys = ", ".join(sorted(data.keys())) or "(none)"
        return f"no `photoTakenTime` key at all — sidecar's top-level keys: {keys}"
    if not isinstance(ptt, dict):
        return f"`photoTakenTime` is a {type(ptt).__name__}, not an object"
    if "timestamp" not in ptt:
        keys = ", ".join(sorted(ptt.keys())) or "(none)"
        return f"`photoTakenTime` has no `timestamp` key — found instead: {keys}"
    try:
        float(ptt["timestamp"])
    except (TypeError, ValueError):
        return "`photoTakenTime.timestamp` isn't a number"
    return None


def _describe_geo_anomaly(data: dict) -> Optional[str]:
    """None if any geoData/geoDataExif block present is shaped as expected
    (a missing block entirely is normal -- not every photo has a location).
    """
    for block_key in ("geoDataExif", "geoData"):
        block = data.get(block_key)
        if block is None:
            continue
        if not isinstance(block, dict):
            return f"`{block_key}` is a {type(block).__name__}, not an object"
        missing = [k for k in ("latitude", "longitude") if k not in block]
        if missing:
            keys = ", ".join(sorted(block.keys())) or "(none)"
            return (
                f"`{block_key}` has no {'/'.join(missing)} — found instead: {keys}"
            )
        try:
            float(block["latitude"])
            float(block["longitude"])
        except (TypeError, ValueError):
            return f"`{block_key}.latitude`/`longitude` aren't numbers"
    return None


_PHOTOS_FROM_YEAR_RE = re.compile(r"^Photos from (\d{4})$", re.IGNORECASE)


def _photos_from_year(path: str) -> Optional[int]:
    """Year embedded in an enclosing "Photos from YYYY" directory name, if any.

    Google assigns this year itself, independent of what this script parses
    out of the JSON sidecar -- a mismatch is a cheap, independent signal of
    a sidecar-matching bug rather than just unusual-but-correct data.
    """
    parent = os.path.basename(os.path.dirname(path))
    match = _PHOTOS_FROM_YEAR_RE.match(parent)
    return int(match.group(1)) if match else None


def write_dryrun_summary(results: list, report_path: str, input_dir: str) -> None:
    """Aggregated dry-run report: patterns and counts, not one line per file.

    Built to answer two questions even across a huge library: are failures
    clustered around a particular cause (a file type, a sidecar-matching
    gap, a recurring exception), and is there sidecar data this script
    isn't preserving yet that shows up often enough to be worth adding.
    """
    updated = [r for r in results if r.outcome == Outcome.UPDATED]
    good_exif = [r for r in results if r.outcome == Outcome.GOOD_EXIF]
    no_json = [r for r in results if r.outcome == Outcome.NO_JSON]
    errors = [r for r in results if r.outcome == Outcome.ERROR]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def rel(r) -> str:  # noqa: E306
        return os.path.relpath(r.path, input_dir)

    lines = [
        "# Google Photos Timestamper — Dry Run Summary",
        f"Generated: {now}",
        "",
        "**DRY RUN — no files were modified, renamed, or moved.** This report "
        "is aggregated by pattern rather than listed file-by-file, so it "
        "stays readable no matter how large the library is.",
        "",
        "## Summary",
        "",
        "| Outcome | Count |",
        "|---------|-------|",
        f"| Would update (EXIF/mtime from JSON) | {len(updated)} |",
        f"| Good EXIF already (no change needed) | {len(good_exif)} |",
        f"| No JSON sidecar found | {len(no_json)} |",
        f"| Errors | {len(errors)} |",
        f"| **Total** | **{len(results)}** |",
        "",
    ]

    if updated:
        n_ts = sum(1 for r in updated if "EXIF timestamp" in r.notes)
        n_gps = sum(1 for r in updated if "EXIF GPS" in r.notes)
        n_both = sum(
            1
            for r in updated
            if "EXIF timestamp" in r.notes and "EXIF GPS" in r.notes
        )
        lines += [
            "## What would change",
            "",
            "| Change | Files |",
            "|--------|-------|",
            f"| Timestamp only | {n_ts - n_both} |",
            f"| GPS only | {n_gps - n_both} |",
            f"| Timestamp + GPS | {n_both} |",
            "",
        ]

    by_ext: dict = {}
    for r in results:
        counts = by_ext.setdefault(
            _file_ext(r.path),
            {"updated": 0, "good_exif": 0, "no_json": 0, "error": 0},
        )
        counts[r.outcome.value] += 1

    if by_ext:
        lines += [
            "## By file type",
            "",
            "| Extension | Would update | Good EXIF | No JSON | Errors | Total |",
            "|-----------|--------------|-----------|---------|--------|-------|",
        ]
        for ext, counts in sorted(
            by_ext.items(), key=lambda kv: sum(kv[1].values()), reverse=True
        ):
            total = sum(counts.values())
            lines.append(
                f"| `{ext}` | {counts['updated']} | {counts['good_exif']} | "
                f"{counts['no_json']} | {counts['error']} | {total} |"
            )
        lines.append("")

    if no_json:
        lines += ["## No JSON sidecar found — by file type", ""]
        by_ext_group: dict = {}
        for r in no_json:
            by_ext_group.setdefault(_file_ext(r.path), []).append(r)
        for ext, group in sorted(
            by_ext_group.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            lines.append(f"- `{ext}`: {len(group)} files")
            examples = sorted(rel(r) for r in group)[:5]
            for ex in examples:
                lines.append(f"    - `{ex}`")
            if len(group) > len(examples):
                lines.append(f"    - … and {len(group) - len(examples)} more")
        lines.append("")

    if errors:
        lines += ["## Errors — grouped by message", ""]
        by_message: dict = {}
        for r in errors:
            by_message.setdefault(r.error or "unknown error", []).append(r)
        for msg, group in sorted(
            by_message.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            lines.append(f"- **{len(group)}×** {msg}")
            examples = sorted(rel(r) for r in group)[:5]
            for ex in examples:
                lines.append(f"    - `{ex}`")
            if len(group) > len(examples):
                lines.append(f"    - … and {len(group) - len(examples)} more")
        lines.append("")

    sidecar_results = [r for r in results if r.sidecar_data]

    if sidecar_results:
        def rel_json(r) -> str:  # noqa: E306
            return os.path.relpath(r.json_path or r.path, input_dir)

        ts_anomalies: dict = {}
        geo_anomalies: dict = {}
        for r in sidecar_results:
            ts_issue = _describe_timestamp_anomaly(r.sidecar_data)
            if ts_issue:
                ts_anomalies.setdefault(ts_issue, []).append(r)
            geo_issue = _describe_geo_anomaly(r.sidecar_data)
            if geo_issue:
                geo_anomalies.setdefault(geo_issue, []).append(r)

        lines += [
            "## Sidecar expectation checks",
            "",
            f"Checked {len(sidecar_results)} sidecars against what this script "
            "assumes: a `photoTakenTime.timestamp` field, and — if a location "
            "block is present at all — numeric `latitude`/`longitude` inside "
            "`geoData`/`geoDataExif`. This is how a schema difference (a "
            "renamed or restructured field in some sidecars) would show up.",
            "",
        ]
        if not ts_anomalies and not geo_anomalies:
            lines.append(
                "No shape anomalies found — every sidecar matched what this "
                "script expects."
            )
            lines.append("")
        else:
            for heading, anomalies in (
                ("Timestamp", ts_anomalies),
                ("GPS", geo_anomalies),
            ):
                if not anomalies:
                    continue
                lines.append(f"**{heading} shape issues:**")
                lines.append("")
                for issue, group in sorted(
                    anomalies.items(), key=lambda kv: len(kv[1]), reverse=True
                ):
                    lines.append(f"- **{len(group)}×** {issue}")
                    examples = sorted(rel_json(r) for r in group)[:5]
                    for ex in examples:
                        lines.append(f"    - `{ex}`")
                    if len(group) > len(examples):
                        lines.append(f"    - … and {len(group) - len(examples)} more")
                lines.append("")

    year_checked_in_folder = []
    year_mismatches: dict = {}
    for r in results:
        if r.assigned_timestamp is None:
            continue
        folder_year = _photos_from_year(r.path)
        if folder_year is None:
            continue
        year_checked_in_folder.append(r)
        assigned_year = datetime.fromtimestamp(
            r.assigned_timestamp, tz=timezone.utc
        ).year
        if assigned_year != folder_year:
            key = f"folder says {folder_year}, assigned timestamp says {assigned_year}"
            year_mismatches.setdefault(key, []).append(r)

    if year_checked_in_folder:
        lines += [
            "## Year-folder cross-check",
            "",
            f"Checked {len(year_checked_in_folder)} files inside a `Photos "
            "from YYYY` folder: does the year in the folder name match the "
            "year of the timestamp this script is about to assign? Google "
            "assigned the folder year itself, independent of what this "
            "script parses from the JSON sidecar -- a mismatch is a strong, "
            "free signal of a sidecar-matching bug, not just unusual data.",
            "",
        ]
        if not year_mismatches:
            lines.append(
                f"No mismatches found across {len(year_checked_in_folder)} "
                "files checked."
            )
            lines.append("")
        else:
            for key, group in sorted(
                year_mismatches.items(), key=lambda kv: len(kv[1]), reverse=True
            ):
                lines.append(f"- **{len(group)}×** {key}")
                examples = sorted(rel(r) for r in group)[:5]
                for ex in examples:
                    lines.append(f"    - `{ex}`")
                if len(group) > len(examples):
                    lines.append(f"    - … and {len(group) - len(examples)} more")
            lines.append("")

    sidecars = [r.sidecar_data for r in sidecar_results]
    if sidecars:
        field_counts: dict = {}
        field_meaningful: dict = {}
        for data in sidecars:
            for key, value in data.items():
                field_counts[key] = field_counts.get(key, 0) + 1
                if _is_meaningful(value):
                    field_meaningful[key] = field_meaningful.get(key, 0) + 1

        lines += [
            "## Sidecar fields seen",
            "",
            f"Surveyed {len(sidecars)} JSON sidecars actually matched during "
            "this run (only a sample if `--sample` was used). "
            "`photoTakenTime`/`geoData`/`geoDataExif` are already used by "
            "this script; everything else is currently ignored — a field "
            "that shows up often here with meaningful values might be worth "
            "preserving too (e.g. as an EXIF/XMP tag).",
            "",
            "| Field | Present | Non-empty/true | Already used |",
            "|-------|---------|-----------------|---------------|",
        ]
        top_fields = sorted(
            field_counts.items(), key=lambda kv: kv[1], reverse=True
        )[:25]
        for key, count in top_fields:
            meaningful = field_meaningful.get(key, 0)
            used = "yes" if key in _HANDLED_SIDECAR_KEYS else ""
            lines.append(f"| `{key}` | {count}/{len(sidecars)} | {meaningful} | {used} |")
        if len(field_counts) > len(top_fields):
            lines.append("")
            lines.append(
                f"_{len(field_counts) - len(top_fields)} further rarely-seen "
                "fields omitted._"
            )
        lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Report written → {report_path}")


# ── Main ───────────────────────────────────────────────────────────────────────

ICONS = {
    Outcome.UPDATED: "✓",
    Outcome.GOOD_EXIF: "~",
    Outcome.NO_JSON: "?",
    Outcome.ERROR: "✗",
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore EXIF timestamps/GPS from Google Takeout JSON sidecars."
    )
    parser.add_argument("directory", help="Path to the Takeout export to process")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without modifying, moving, or renaming "
        "any files",
    )
    parser.add_argument(
        "--sample",
        type=float,
        default=None,
        metavar="PERCENT",
        help="Only process a random PERCENT of matched files. Requires "
        "--dry-run; useful for a fast preview before running on a large "
        "library",
    )
    return parser


def find_input_files(input_dir: str) -> list:
    file_paths = []
    for dirpath, dirnames, filenames in os.walk(input_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in OUTPUT_DIRS]
        for filename in filenames:
            if (
                filename.endswith(".json")
                or filename == ".DS_Store"
                or filename.endswith(".md")
            ):
                continue
            file_paths.append(os.path.join(dirpath, filename))
    return file_paths


def main() -> None:
    if not shutil.which("exiftool"):
        sys.exit("exiftool is required.  Install with: brew install exiftool")

    args = build_arg_parser().parse_args()

    if args.sample is not None:
        if not args.dry_run:
            sys.exit("--sample can only be used together with --dry-run")
        if not 0 < args.sample <= 100:
            sys.exit("--sample must be greater than 0 and at most 100")

    input_dir = args.directory
    report_name = (
        "timestamper_dryrun_report.md" if args.dry_run else "timestamper_report.md"
    )
    report_path = os.path.join(input_dir, report_name)

    file_paths = find_input_files(input_dir)

    if args.sample is not None:
        sample_size = max(1, round(len(file_paths) * args.sample / 100))
        sample_size = min(sample_size, len(file_paths))
        file_paths = random.sample(file_paths, sample_size)
        print(f"Sampling {sample_size} of the matched files ({args.sample:g}%)\n")

    results: list = []

    for file_path in file_paths:
        result = process_file(file_path, dry_run=args.dry_run)
        results.append(result)
        detail = result.notes or result.error or result.outcome.value
        print(f"  {ICONS[result.outcome]}  {os.path.basename(file_path)}  —  {detail}")

    print()

    if args.dry_run:
        write_dryrun_summary(results, report_path, input_dir)
    else:
        write_report(results, report_path, input_dir)

    n_good = sum(
        1 for r in results if r.outcome in (Outcome.UPDATED, Outcome.GOOD_EXIF)
    )
    n_bad = sum(1 for r in results if r.outcome in (Outcome.NO_JSON, Outcome.ERROR))

    if args.dry_run:
        print("\nDry run only — no files were modified, renamed, or moved.")
        print(f"→ would move to ready/     {n_good} files")
        print("→ would move to sidecars/  matched JSON sidecars")
        print(f"→ would move to problems/  {n_bad} files")
    else:
        organise_files(input_dir, results, report_path)
        print(f"\n→ ready/  {n_good} files")
        print("→ sidecars/   matched JSON sidecars")
        print(f"→ problems/   {n_bad} files")


if __name__ == "__main__":
    main()
