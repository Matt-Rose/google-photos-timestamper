"""Integration tests for video (QuickTime container) handling.

process_file() reports success for videos today, but write_exif_tags() only
writes EXIF/XMP date tags -- it never touches the QuickTime CreateDate /
Track*/Media* date tags that Finder and Apple Photos actually read for a
video's date. test_process_file_sets_quicktime_create_date_for_video below
is a known-failing regression test documenting that bug; it should start
passing once write_exif_tags is fixed to also set the QuickTime tags.
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
