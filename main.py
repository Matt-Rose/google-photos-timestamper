import json
import os
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


# ── Startup check ──────────────────────────────────────────────────────────────

if not shutil.which("exiftool"):
    sys.exit("exiftool is required.  Install with: brew install exiftool")


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

    raise NoJsonFileFoundError(f"Could not find JSON sidecar for {image_path}")


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
    """Write DateTimeOriginal/Digitized and/or GPS EXIF tags via exiftool.

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

    args.append(path)

    result = subprocess.run(args, capture_output=True, timeout=30)
    if result.returncode != 0:
        # Retry with -m to suppress minor format-mismatch errors (e.g. a JPEG
        # with a .HEIC extension that exiftool refuses to process normally).
        retry = args[:1] + ["-m"] + args[1:]
        subprocess.run(retry, check=True, capture_output=True, timeout=30)


# ── Per-file processing ────────────────────────────────────────────────────────


def process_file(path: str) -> FileResult:
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
            os.utime(path, (exif_dt.timestamp(), exif_dt.timestamp()))
            # Still locate the sidecar so organise_files can move it to sidecars/.
            try:
                _, json_path = get_json_path_and_data(path)
            except NoJsonFileFoundError:
                json_path = None
            return FileResult(
                path, Outcome.GOOD_EXIF, "mtime synced from existing EXIF", json_path
            )

        # Try to find the JSON sidecar.
        try:
            json_data, json_path = get_json_path_and_data(path)
        except NoJsonFileFoundError:
            if ts_good:
                # Good timestamp but no GPS — still usable; sync mtime.
                assert exif_dt is not None  # guaranteed by ts_good
                os.utime(path, (exif_dt.timestamp(), exif_dt.timestamp()))
                return FileResult(
                    path,
                    Outcome.GOOD_EXIF,
                    "good EXIF timestamp (no GPS in sidecar); mtime synced",
                )
            return FileResult(path, Outcome.NO_JSON)

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
        os.utime(path, (mtime_ts, mtime_ts))
        note_parts = ["mtime"]

        # Only write what actually needs updating — skip timestamp if already good,
        # skip GPS if already good or no usable GPS found in JSON.
        write_ts  = not ts_good
        write_gps = not gps_good and lat is not None

        if write_ts or write_gps:
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

        return FileResult(path, Outcome.UPDATED, ", ".join(note_parts), json_path)

    except Exception as e:
        return FileResult(path, Outcome.ERROR, error=str(e))


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


# ── Main ───────────────────────────────────────────────────────────────────────

if len(sys.argv) < 2:
    sys.exit("Usage: python main.py <directory>")

input_dir = sys.argv[1]
report_path = os.path.join(input_dir, "timestamper_report.md")

ICONS = {
    Outcome.UPDATED: "✓",
    Outcome.GOOD_EXIF: "~",
    Outcome.NO_JSON: "?",
    Outcome.ERROR: "✗",
}

results: list = []

for dirpath, dirnames, filenames in os.walk(input_dir, topdown=True):
    dirnames[:] = [d for d in dirnames if d not in OUTPUT_DIRS]
    for filename in filenames:
        if (
            filename.endswith(".json")
            or filename == ".DS_Store"
            or filename.endswith(".md")
        ):
            continue
        file_path = os.path.join(dirpath, filename)
        result = process_file(file_path)
        results.append(result)
        detail = result.notes or result.error or result.outcome.value
        print(f"  {ICONS[result.outcome]}  {filename}  —  {detail}")

print()

write_report(results, report_path, input_dir)
organise_files(input_dir, results, report_path)

n_good = sum(1 for r in results if r.outcome in (Outcome.UPDATED, Outcome.GOOD_EXIF))
n_bad = sum(1 for r in results if r.outcome in (Outcome.NO_JSON, Outcome.ERROR))

print(f"\n→ ready/  {n_good} files")
print("→ sidecars/   matched JSON sidecars")
print(f"→ problems/   {n_bad} files")
