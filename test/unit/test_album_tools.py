"""Unit tests for the pure logic in the album-preparation tools.

The exiftool/ffmpeg/Photos paths need real binaries and real files; what is
testable in isolation is the matching and classification logic, which is
where the bugs actually were.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import album_survey  # noqa: E402
import recover_dates  # noqa: E402


class TestDateFromFilename:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("PXL_20230531_145342087.TS.mp4", "2023-05-31 14:53:42"),
            ("IMG_20180225_113601.jpg", "2018-02-25 11:36:01"),
            ("00030IMG_00030_BURST20190804121948_COVER.jpg", "2019-08-04 12:19:48"),
            ("IMG-20180225-WA0014.jpg", "2018-02-25 12:00:00"),
        ],
    )
    def test_extracts(self, name, expected):
        assert recover_dates.date_from_filename(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            # A subject year in front of the scan date: the leftmost 8-digit
            # window is 2013-21-07, which is not a date. Shapes taken from real
            # scanned-archive filenames, with the descriptions genericised.
            "Msubject description 201321072014.jpg",
            "J subject description 2 july 200021072014.jpg",
            "Ithree subject names 209072014.jpg",
        ],
    )
    def test_rejects_impossible_dates(self, name):
        """Must never return e.g. 2090-72-01; apply() used to crash on it."""
        stamp = recover_dates.date_from_filename(name)
        if stamp is not None:
            datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")   # parses or fails

    def test_keeps_looking_past_an_invalid_match(self):
        """An impossible leftmost match must not mask a valid one after it."""
        assert (
            recover_dates.date_from_filename("scan20139901_PXL_20230531_145342087.jpg")
            == "2023-05-31 14:53:42"
        )

    @pytest.mark.parametrize("name", ["IMG_1915.JPG", "koala_13.png", "Kitchen plan.png"])
    def test_no_false_positives(self, name):
        assert recover_dates.date_from_filename(name) is None


class TestDateOutOfRange:
    @pytest.mark.parametrize(
        "value",
        [
            # Real corrupt QuickTime CreateDate values. Photos rejects these
            # with "date value out of range" and fails the whole import chunk.
            "108866:11:23 08:30:20",
            "29946:02:01 16:58:48",
            "0000:00:00 00:00:00",
        ],
    )
    def test_rejects(self, value):
        assert album_survey.date_out_of_range(value) is True

    @pytest.mark.parametrize(
        "value",
        ["2016:11:21 22:42:00", "1985:06:01 00:00:00", "-", ""],
    )
    def test_accepts_plausible_and_empty(self, value):
        assert album_survey.date_out_of_range(value) is False


class TestContentMismatch:
    def _write(self, tmp_path, name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)

    def test_jpeg_named_heic_is_caught(self, tmp_path):
        """Google serves JPEG bytes under .HEIC; exiftool then refuses writes."""
        path = self._write(tmp_path, "IMG_1.HEIC", b"\xff\xd8\xff\xe0" + b"\x00" * 8)
        assert album_survey.content_mismatch(path) == "JPEG"

    def test_real_heic_passes(self, tmp_path):
        """HEIC is ISO-BMFF: 'ftyp' sits at offset 4, not offset 0."""
        path = self._write(tmp_path, "IMG_2.HEIC", b"\x00\x00\x00\x18ftypheic")
        assert album_survey.content_mismatch(path) is None

    def test_jpeg_named_png_is_caught(self, tmp_path):
        path = self._write(tmp_path, "shot.png", b"\xff\xd8\xff\xe0" + b"\x00" * 8)
        assert album_survey.content_mismatch(path) == "JPEG"

    def test_real_png_passes(self, tmp_path):
        path = self._write(tmp_path, "shot.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)
        assert album_survey.content_mismatch(path) is None

    def test_unknown_extension_ignored(self, tmp_path):
        path = self._write(tmp_path, "clip.mov", b"whatever1234")
        assert album_survey.content_mismatch(path) is None


class TestAspect:
    def test_survives_downscaling(self):
        """The download is often a downscale of the reference copy."""
        full = recover_dates.aspect(3024, 4032)
        small = recover_dates.aspect(640, 852)
        assert abs(full - small) <= recover_dates.ASPECT_TOLERANCE

    def test_rejects_a_crop(self):
        """A cropped edit must not be accepted as the same image."""
        original = recover_dates.aspect(1440, 1800)
        cropped = recover_dates.aspect(1437, 1107)
        assert abs(original - cropped) > recover_dates.ASPECT_TOLERANCE

    def test_orientation_independent(self):
        assert recover_dates.aspect(640, 852) == recover_dates.aspect(852, 640)

    @pytest.mark.parametrize("w,h", [(0, 100), (100, 0), ("-", "-"), (None, None)])
    def test_bad_input(self, w, h):
        assert recover_dates.aspect(w, h) is None


class TestHasUsableDate:
    @pytest.mark.parametrize(
        "dto,create,expected",
        [
            ("2025:01:26 18:38:06", "-", True),
            ("-", "2025:01:26 18:38:06", True),
            ("-", "-", False),
            ("0000:00:00 00:00:00", "-", False),   # seen on Pixel .TS.mp4 files
            ("1970:01:01 01:00:00", "-", False),   # epoch, worse than nothing
            ("-", "0000:00:00 00:00:00", False),
        ],
    )
    def test_classification(self, dto, create, expected):
        row = {"dto": dto, "create": create}
        assert album_survey.has_usable_date(row) is expected


class TestGrouping:
    def test_pair_detected(self):
        groups = album_survey.group_files(["IMG_7413.HEIC", "IMG_7413.MP4"])
        assert album_survey.is_pair(groups["IMG_7413"])

    def test_two_stills_are_not_a_pair(self):
        groups = album_survey.group_files(["a.jpg", "b.jpg"])
        assert not album_survey.is_pair(groups["a"])

    def test_expected_asset_count_is_group_count(self):
        names = ["IMG_1.HEIC", "IMG_1.MP4", "IMG_2.HEIC", "solo.jpg"]
        assert len(album_survey.group_files(names)) == 3


def test_rms_identical_is_zero():
    sig = [0.5, -0.5, 1.0, -1.0]
    assert recover_dates.rms(sig, sig) == 0.0
