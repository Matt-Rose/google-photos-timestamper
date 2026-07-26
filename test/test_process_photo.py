"""Integration tests that exercise process_file() against a real image file
through the real exiftool binary (no mocking) so we catch anything a pure
logic test would miss.

test_process_file_sets_mtime_to_photo_taken_time_when_exif_also_written is a
known-failing regression test: process_file() calls os.utime() *before*
write_exif_tags(), but exiftool -overwrite_original rewrites the file and
resets its mtime to wall-clock "now" on any write -- so for every file that
actually needs an EXIF write, the resulting mtime is "whenever the script
ran", not the photo's real taken time.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

from conftest import write_sidecar
from main import Outcome, process_file


def _read_tags(path, *tags):
    result = subprocess.run(
        ["exiftool", "-json", "-n", *[f"-{t}" for t in tags], path],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)[0]


OLD_TIMESTAMP = int(datetime(2020, 1, 15, 10, 30, tzinfo=timezone.utc).timestamp())


def test_process_file_writes_exif_and_mtime_from_sidecar(sample_jpg):
    photo = sample_jpg()
    write_sidecar(
        photo + ".json", OLD_TIMESTAMP, lat=51.5, lon=-0.12, alt=10.0
    )

    result = process_file(photo)

    assert result.outcome == Outcome.UPDATED
    assert result.json_path == photo + ".json"

    tags = _read_tags(photo, "DateTimeOriginal", "GPSLatitude", "GPSLongitude")
    assert tags["DateTimeOriginal"] == "2020:01:15 10:30:00"
    assert round(tags["GPSLatitude"], 2) == 51.5
    assert round(tags["GPSLongitude"], 2) == -0.12


def test_process_file_sets_mtime_to_photo_taken_time_when_exif_also_written(
    sample_jpg,
):
    photo = sample_jpg()
    write_sidecar(photo + ".json", OLD_TIMESTAMP, lat=51.5, lon=-0.12, alt=10.0)

    process_file(photo)

    assert int(os.path.getmtime(photo)) == OLD_TIMESTAMP


def test_process_file_leaves_good_exif_alone(tmp_path, sample_jpg):
    photo = sample_jpg()
    old_dt = "2019:06:01 08:00:00"
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            f"-DateTimeOriginal={old_dt}",
            "-GPSLatitude=51.5",
            "-GPSLatitudeRef=N",
            "-GPSLongitude=-0.12",
            "-GPSLongitudeRef=W",
            photo,
        ],
        check=True,
        capture_output=True,
    )

    result = process_file(photo)

    assert result.outcome == Outcome.GOOD_EXIF
    tags = _read_tags(photo, "DateTimeOriginal")
    assert tags["DateTimeOriginal"] == old_dt


def test_process_file_no_sidecar_and_no_good_exif_is_no_json(sample_jpg):
    photo = sample_jpg()
    result = process_file(photo)
    assert result.outcome == Outcome.NO_JSON
