import json
import os
import re

import pytest

from main import (
    NoJsonFileFoundError,
    _sidecar_candidates,
    get_alike_json,
    get_alike_regex,
    get_alike_regex_with_duplication,
    get_json_path_and_data,
    move_duplication_string,
)


def test_move_duplication_string_moves_paren_suffix():
    assert move_duplication_string("IMG(1).jpg") == "IMG.jpg(1)"


def test_move_duplication_string_no_parens_unchanged():
    assert move_duplication_string("IMG.jpg") == "IMG.jpg"


def test_get_alike_regex_matches_macos_timestamp_variant():
    pattern = get_alike_regex("IMG.jpg")
    assert re.match(pattern, "IMG 1.09.32 PM.jpg.json") is not None


def test_get_alike_regex_does_not_match_unrelated_name():
    pattern = get_alike_regex("IMG.jpg")
    assert re.match(pattern, "OtherFile.jpg.json") is None


def test_get_alike_regex_with_duplication_matches_moved_timestamp_variant():
    pattern = get_alike_regex_with_duplication("IMG.jpg(1)")
    assert re.match(pattern, "IMG.jpg(1) 1.09.32 PM.json") is not None


def test_get_alike_json_finds_macos_timestamp_variant(tmp_path):
    image = tmp_path / "IMG.jpg"
    image.write_bytes(b"x")
    sidecar = tmp_path / "IMG 1.09.32 PM.jpg.json"
    sidecar.write_text("{}")

    found = get_alike_json(str(image))
    assert found == str(sidecar)


def test_get_alike_json_returns_none_when_nothing_matches(tmp_path):
    image = tmp_path / "IMG.jpg"
    image.write_bytes(b"x")
    assert get_alike_json(str(image)) is None


def test_sidecar_candidates_includes_plain_and_supplemental_forms():
    candidates = _sidecar_candidates("photo.jpg", "/dir")
    names = {os.path.basename(c) for c in candidates}
    assert "photo.jpg.json" in names
    assert "photo.jpg.supplemental-metadata.json" in names
    assert "photo.json" in names


def test_sidecar_candidates_includes_truncated_variant_for_long_names():
    long_name = "a" * 60 + ".jpg"
    candidates = _sidecar_candidates(long_name, "/dir")
    truncated = [os.path.basename(c) for c in candidates if len(c) < 60]
    assert any(name.endswith(".json") and len(name) <= 51 for name in truncated)


def test_get_json_path_and_data_matches_exact_name(tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"x")
    sidecar = tmp_path / "photo.jpg.json"
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "100"}}))

    data, path = get_json_path_and_data(str(image))
    assert path == str(sidecar)
    assert data["photoTakenTime"]["timestamp"] == "100"


def test_get_json_path_and_data_matches_supplemental_metadata_form(tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"x")
    sidecar = tmp_path / "photo.jpg.supplemental-metadata.json"
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "200"}}))

    data, path = get_json_path_and_data(str(image))
    assert path == str(sidecar)


def test_get_json_path_and_data_strips_edited_suffix(tmp_path):
    image = tmp_path / "photo-edited.jpg"
    image.write_bytes(b"x")
    sidecar = tmp_path / "photo.jpg.json"
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "300"}}))

    data, path = get_json_path_and_data(str(image))
    assert path == str(sidecar)


def test_get_json_path_and_data_matches_moved_duplication_string(tmp_path):
    image = tmp_path / "photo(1).jpg"
    image.write_bytes(b"x")
    sidecar = tmp_path / "photo.jpg(1).json"
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "400"}}))

    data, path = get_json_path_and_data(str(image))
    assert path == str(sidecar)


def test_get_json_path_and_data_raises_when_nothing_found(tmp_path):
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"x")

    with pytest.raises(NoJsonFileFoundError):
        get_json_path_and_data(str(image))
