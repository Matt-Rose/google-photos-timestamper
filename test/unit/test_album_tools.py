"""Unit tests for the pure logic in the album-preparation tools.

The exiftool/ffmpeg/Photos paths need real binaries and real files; what is
testable in isolation is the matching and classification logic, which is
where the bugs actually were.
"""

import os
import sys

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

    @pytest.mark.parametrize("name", ["IMG_1915.JPG", "koala_13.png", "Kitchen plan.png"])
    def test_no_false_positives(self, name):
        assert recover_dates.date_from_filename(name) is None


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
