"""Integration tests for video (QuickTime container) handling.

write_exif_tags() also sets the QuickTime CreateDate/ModifyDate/Track*/
Media* date tags (in addition to EXIF/XMP), since those are what Finder and
Apple Photos actually read for a video's date.
"""

import json
import subprocess
from datetime import datetime, timezone

from conftest import write_sidecar
from main import Outcome, process_file

OLD_TIMESTAMP = int(datetime(2020, 1, 15, 10, 30, tzinfo=timezone.utc).timestamp())


def _read_tags(path, *tags):
    result = subprocess.run(
        ["exiftool", "-json", "-n", *[f"-{t}" for t in tags], path],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)[0]


def test_process_file_reports_updated_for_video(sample_mov):
    video = sample_mov()
    write_sidecar(video + ".json", OLD_TIMESTAMP)

    result = process_file(video)

    assert result.outcome == Outcome.UPDATED


def test_process_file_sets_quicktime_create_date_for_video(sample_mov):
    video = sample_mov()
    write_sidecar(video + ".json", OLD_TIMESTAMP)

    process_file(video)

    tags = _read_tags(video, "QuickTime:CreateDate")
    assert tags.get("CreateDate") == "2020:01:15 10:30:00"


def test_process_file_sets_quicktime_gps_for_video(sample_mov):
    video = sample_mov()
    write_sidecar(video + ".json", OLD_TIMESTAMP, lat=51.5, lon=-0.12, alt=10.0)

    process_file(video)

    tags = _read_tags(video, "QuickTime:GPSCoordinates")
    coords = tags.get("GPSCoordinates", "")
    parts = [float(p) for p in coords.split()]
    assert round(parts[0], 2) == 51.5
    assert round(parts[1], 2) == -0.12
