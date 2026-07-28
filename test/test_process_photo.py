"""Integration tests that exercise process_file() against a real image file
through the real exiftool binary (no mocking) so we catch anything a pure
logic test would miss.
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
    assert result.assigned_timestamp == OLD_TIMESTAMP

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


def test_process_file_reports_clear_error_and_keeps_sidecar_data_on_shape_drift(
    sample_jpg,
):
    """A malformed sidecar (renamed sub-key) must produce a specific error
    message and still carry sidecar_data, so the dry-run report's
    expectation-check section can flag exactly what deviated -- not just a
    bare KeyError with no diagnostic value."""
    photo = sample_jpg()
    with open(photo + ".json", "w") as f:
        json.dump({"photoTakenTime": {"time-stamp": str(OLD_TIMESTAMP)}}, f)

    result = process_file(photo)

    assert result.outcome == Outcome.ERROR
    assert "photoTakenTime" in result.error
    assert "time-stamp" in result.error
    assert result.sidecar_data == {"photoTakenTime": {"time-stamp": str(OLD_TIMESTAMP)}}
