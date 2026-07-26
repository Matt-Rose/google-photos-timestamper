import os

import pytest

from main import build_arg_parser, find_input_files


def test_arg_parser_requires_directory():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args([])


def test_arg_parser_defaults():
    args = build_arg_parser().parse_args(["some/dir"])
    assert args.directory == "some/dir"
    assert args.dry_run is False
    assert args.sample is None


def test_arg_parser_dry_run_and_sample():
    args = build_arg_parser().parse_args(["some/dir", "--dry-run", "--sample", "5"])
    assert args.dry_run is True
    assert args.sample == 5.0


def test_find_input_files_excludes_json_ds_store_md_and_output_dirs(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(b"x")
    (tmp_path / "photo.jpg.json").write_text("{}")
    (tmp_path / ".DS_Store").write_bytes(b"")
    (tmp_path / "timestamper_report.md").write_text("report")
    os.makedirs(tmp_path / "ready")
    (tmp_path / "ready" / "already_done.jpg").write_bytes(b"x")

    found = find_input_files(str(tmp_path))

    assert found == [str(tmp_path / "photo.jpg")]
