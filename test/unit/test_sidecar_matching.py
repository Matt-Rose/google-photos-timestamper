import json
import os
import re

import pytest

from main import (
    NoJsonFileFoundError,
    _dedup_supplemental_candidate,
    _find_live_photo_sibling_json,
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


def test_live_photo_sibling_extensionless_video_variant(tmp_path):
    # Some Live Photo video components are exported with no extension at
    # all (confirmed via `file` on real examples to still be QuickTime .MOV
    # content), not just .MP4/.MOV.
    (tmp_path / "IMG_8326.HEIC").write_bytes(b"x")
    (tmp_path / "IMG_8326").write_bytes(b"x")
    (tmp_path / "IMG_8326.HEIC.supplemental-metadata.json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "900"}})
    )

    data, path = get_json_path_and_data(str(tmp_path / "IMG_8326"))

    assert data["photoTakenTime"]["timestamp"] == "900"
    assert path == str(tmp_path / "IMG_8326.HEIC.supplemental-metadata.json")


def test_live_photo_sibling_iphone_pattern(tmp_path):
    (tmp_path / "IMG_6375.HEIC").write_bytes(b"x")
    (tmp_path / "IMG_6375.MP4").write_bytes(b"x")
    (tmp_path / "IMG_6375.HEIC.supplemental-metadata.json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "500"}})
    )

    data, path = get_json_path_and_data(str(tmp_path / "IMG_6375.MP4"))

    assert data["photoTakenTime"]["timestamp"] == "500"
    assert path == str(tmp_path / "IMG_6375.HEIC.supplemental-metadata.json")


def test_live_photo_sibling_android_motion_photo_pattern(tmp_path):
    video = tmp_path / "PXL_20230802_095819065.MP"
    photo = tmp_path / "PXL_20230802_095819065.MP.jpg"
    sidecar = tmp_path / "PXL_20230802_095819065.MP.jpg.supplemental-metadata.json"
    video.write_bytes(b"x")
    photo.write_bytes(b"x")
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "600"}}))

    data, path = get_json_path_and_data(str(video))

    assert data["photoTakenTime"]["timestamp"] == "600"
    assert path == str(sidecar)


def test_live_photo_sibling_does_not_match_unrelated_numeric_prefix(tmp_path):
    # IMG_100.MP4 must not match IMG_1000.HEIC just because it's a string prefix.
    (tmp_path / "IMG_100.MP4").write_bytes(b"x")
    (tmp_path / "IMG_1000.HEIC").write_bytes(b"x")
    (tmp_path / "IMG_1000.HEIC.supplemental-metadata.json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "700"}})
    )

    assert _find_live_photo_sibling_json(str(tmp_path / "IMG_100.MP4")) is None
    with pytest.raises(NoJsonFileFoundError):
        get_json_path_and_data(str(tmp_path / "IMG_100.MP4"))


def test_live_photo_sibling_not_used_for_non_video_extensions(tmp_path):
    # A photo with no sidecar shouldn't borrow from an unrelated sibling.
    (tmp_path / "IMG_1.jpg").write_bytes(b"x")
    (tmp_path / "IMG_1.png").write_bytes(b"x")
    (tmp_path / "IMG_1.png.supplemental-metadata.json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "800"}})
    )

    assert _find_live_photo_sibling_json(str(tmp_path / "IMG_1.jpg")) is None


def test_live_photo_sibling_skips_sibling_with_no_sidecar_of_its_own(tmp_path):
    (tmp_path / "IMG_2.MOV").write_bytes(b"x")
    (tmp_path / "IMG_2.HEIC").write_bytes(b"x")  # no sidecar for this either

    assert _find_live_photo_sibling_json(str(tmp_path / "IMG_2.MOV")) is None


def test_dedup_supplemental_candidate_builds_expected_path():
    candidate = _dedup_supplemental_candidate("IMG_2509(1).JPG", "/dir")
    assert candidate == "/dir/IMG_2509.JPG.supplemental-metadata(1).json"


def test_dedup_supplemental_candidate_none_without_duplicate_suffix():
    assert _dedup_supplemental_candidate("IMG_2509.JPG", "/dir") is None


def test_get_json_path_and_data_matches_new_format_duplicate_naming(tmp_path):
    # Real-world shape found in Sophia's export: the original and its
    # duplicate both have "supplemental-metadata" sidecars, but the
    # duplicate's "(1)" lands after "supplemental-metadata", not in the
    # same position as on the media filename.
    (tmp_path / "IMG_2509.JPG").write_bytes(b"x")
    (tmp_path / "IMG_2509.JPG.supplemental-metadata.json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "100"}})
    )
    (tmp_path / "IMG_2509(1).JPG").write_bytes(b"x")
    (tmp_path / "IMG_2509.JPG.supplemental-metadata(1).json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "200"}})
    )

    data, path = get_json_path_and_data(str(tmp_path / "IMG_2509(1).JPG"))

    assert data["photoTakenTime"]["timestamp"] == "200"
    assert path == str(tmp_path / "IMG_2509.JPG.supplemental-metadata(1).json")


def test_live_photo_sibling_resolves_new_format_duplicate_naming(tmp_path):
    # The video half of a duplicated Live Photo pair: the fallback must
    # chain through the new-format duplicate-naming fix too.
    (tmp_path / "IMG_2509(1).JPG").write_bytes(b"x")
    (tmp_path / "IMG_2509(1).MP4").write_bytes(b"x")
    (tmp_path / "IMG_2509.JPG.supplemental-metadata(1).json").write_text(
        json.dumps({"photoTakenTime": {"timestamp": "300"}})
    )

    data, path = get_json_path_and_data(str(tmp_path / "IMG_2509(1).MP4"))

    assert data["photoTakenTime"]["timestamp"] == "300"
    assert path == str(tmp_path / "IMG_2509.JPG.supplemental-metadata(1).json")
