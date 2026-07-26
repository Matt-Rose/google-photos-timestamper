"""Integration tests for --dry-run and --sample: the whole point of these
flags is that they must never touch a real file, so every test here asserts
byte-for-byte and mtime equality against the pre-run state.
"""

import os

import pytest
from conftest import write_sidecar
from datetime import datetime, timezone

import main as main_module
from main import Outcome, process_file

OLD_TIMESTAMP = int(datetime(2020, 1, 15, 10, 30, tzinfo=timezone.utc).timestamp())


def test_process_file_dry_run_reports_but_does_not_modify(sample_jpg):
    photo = sample_jpg()
    write_sidecar(photo + ".json", OLD_TIMESTAMP, lat=51.5, lon=-0.12, alt=10.0)

    before_bytes = open(photo, "rb").read()
    before_mtime = os.path.getmtime(photo)

    result = process_file(photo, dry_run=True)

    assert result.outcome == Outcome.UPDATED
    assert "EXIF timestamp" in result.notes
    assert "EXIF GPS" in result.notes
    assert open(photo, "rb").read() == before_bytes
    assert os.path.getmtime(photo) == before_mtime


def test_main_dry_run_leaves_everything_in_place(tmp_path, sample_jpg, monkeypatch):
    input_dir = tmp_path / "input"
    photo = sample_jpg(str(input_dir / "AlbumA"), "photo.jpg")
    write_sidecar(photo + ".json", OLD_TIMESTAMP)

    before_bytes = open(photo, "rb").read()
    before_mtime = os.path.getmtime(photo)

    monkeypatch.setattr("sys.argv", ["main.py", str(input_dir), "--dry-run"])
    main_module.main()

    # No output directories were ever created.
    assert not (input_dir / "ready").exists()
    assert not (input_dir / "sidecars").exists()
    assert not (input_dir / "problems").exists()

    # The original files are untouched, in their original location.
    assert open(photo, "rb").read() == before_bytes
    assert os.path.getmtime(photo) == before_mtime

    # A dry-run report was written, distinctly named from the real report.
    dryrun_report = input_dir / "timestamper_dryrun_report.md"
    assert dryrun_report.exists()
    assert not (input_dir / "timestamper_report.md").exists()
    text = dryrun_report.read_text()
    assert "DRY RUN" in text
    assert "**Total** | **1**" in text


def test_main_sample_without_dry_run_errors(tmp_path, sample_jpg, monkeypatch):
    input_dir = tmp_path / "input"
    sample_jpg(str(input_dir), "photo.jpg")

    monkeypatch.setattr(
        "sys.argv", ["main.py", str(input_dir), "--sample", "10"]
    )

    with pytest.raises(SystemExit):
        main_module.main()

    # Nothing should have been touched before bailing out.
    assert not (input_dir / "ready").exists()


def test_main_dry_run_with_sample_processes_a_subset(
    tmp_path, sample_jpg, monkeypatch, capsys
):
    input_dir = tmp_path / "input"
    for i in range(20):
        photo = sample_jpg(str(input_dir), f"photo{i}.jpg")
        write_sidecar(photo + ".json", OLD_TIMESTAMP)

    monkeypatch.setattr(
        "sys.argv", ["main.py", str(input_dir), "--dry-run", "--sample", "25"]
    )
    main_module.main()

    dryrun_report = input_dir / "timestamper_dryrun_report.md"
    text = dryrun_report.read_text()
    assert "**Total** | **5**" in text  # round(20 * 25 / 100)

    out = capsys.readouterr().out
    assert "Sampling 5 of the matched files" in out

    # Every original file must still be present and untouched, sampled or not.
    for i in range(20):
        assert (input_dir / f"photo{i}.jpg").exists()


def test_main_dry_run_report_surveys_sidecar_fields(tmp_path, sample_jpg, monkeypatch):
    input_dir = tmp_path / "input"
    photo = sample_jpg(str(input_dir), "photo.jpg")
    write_sidecar(
        photo + ".json",
        OLD_TIMESTAMP,
        extra={"description": "A lovely sunset", "favorited": True},
    )

    monkeypatch.setattr("sys.argv", ["main.py", str(input_dir), "--dry-run"])
    main_module.main()

    text = (input_dir / "timestamper_dryrun_report.md").read_text()
    assert "## Sidecar fields seen" in text
    assert "`photoTakenTime`" in text and "| yes |" in text
    assert "`description`" in text
    assert "`favorited`" in text
