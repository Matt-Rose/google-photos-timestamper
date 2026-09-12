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
import prune_imported  # noqa: E402
import sharing_status  # noqa: E402
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


class TestSharingStatus:
    def test_column_match_rejects_the_key_asset_table(self):
        """Z_32KEYASSETS has a column containing both ALBUM and ASSET.

        A substring test picks that table and every album then reports a
        handful of members instead of its real count -- a quiet wrong answer.
        """
        key_asset_cols = ["Z_32ALBUMSBEINGKEYASSETS", "Z_3KEYASSETS", "Z_FOK_3KEYASSETS"]
        assert not any(sharing_status.ALBUM_COL.match(c) for c in key_asset_cols)
        assert not any(sharing_status.ASSET_COL.match(c) for c in key_asset_cols)

    def test_column_match_accepts_the_membership_table(self):
        cols = ["Z_33ALBUMS", "Z_3ASSETS", "Z_FOK_3ASSETS"]
        assert [c for c in cols if sharing_status.ALBUM_COL.match(c)] == ["Z_33ALBUMS"]
        assert [c for c in cols if sharing_status.ASSET_COL.match(c)] == ["Z_3ASSETS"]

    def test_private_items_outrank_everything(self):
        assert sharing_status.classify(220, 17, 99, []) == sharing_status.HAS_PRIVATE

    def test_no_shared_album(self):
        assert sharing_status.classify(50, 50, None, []) == sharing_status.NO_SHARED_ALBUM

    def test_shared_album_incomplete(self):
        got = sharing_status.classify(165, 165, 7, ["IMG_1.HEIC"])
        assert got == sharing_status.SHARED_INCOMPLETE

    def test_deletable_only_when_everything_lines_up(self):
        assert sharing_status.classify(8, 8, 7, []) == sharing_status.DELETABLE

    def test_empty_album_is_not_reported_as_having_private_items(self):
        """An album with no assets must not read as 0/0 still private."""
        assert sharing_status.classify(0, 0, None, []) == sharing_status.NO_SHARED_ALBUM


class TestMediaFiles:
    def test_finds_media_and_skips_everything_else(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.JPG").write_bytes(b"x")
        (tmp_path / "sub" / "a.mp4").write_bytes(b"x")
        (tmp_path / "notes.txt").write_bytes(b"x")
        (tmp_path / "sidecar.json").write_bytes(b"x")
        assert prune_imported.media_files(str(tmp_path)) == [
            os.path.join("sub", "a.JPG"),
            os.path.join("sub", "a.mp4"),
        ]

    def test_skips_dotfiles_and_dot_directories(self, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "a.jpg").write_bytes(b"x")
        (tmp_path / "._resource.jpg").write_bytes(b"x")
        (tmp_path / "real.jpg").write_bytes(b"x")
        assert prune_imported.media_files(str(tmp_path)) == ["real.jpg"]


class TestLedger:
    def test_round_trips_decisions(self, tmp_path):
        ledger = tmp_path / "l.tsv"
        ledger.write_text("a.jpg\tpresent\thash\tUUID-1\nb.jpg\tnew\t\t\n")
        assert prune_imported.read_ledger(str(ledger)) == {
            "a.jpg": "present",
            "b.jpg": "new",
        }

    def test_missing_ledger_is_empty_not_an_error(self, tmp_path):
        assert prune_imported.read_ledger(str(tmp_path / "nope.tsv")) == {}

    def test_ignores_a_truncated_final_line(self, tmp_path):
        """An interrupted run can leave a partial line; it must not crash."""
        ledger = tmp_path / "l.tsv"
        ledger.write_text("a.jpg\tpresent\thash\tUUID-1\nb.jp")
        assert prune_imported.read_ledger(str(ledger)) == {"a.jpg": "present"}


class TestOrphanedVideos:
    def test_video_is_orphaned_when_its_still_is_present(self):
        files = ["IMG_1.HEIC", "IMG_1.MOV"]
        decisions = {"IMG_1.HEIC": prune_imported.PRESENT, "IMG_1.MOV": prune_imported.NEW}
        assert prune_imported.orphaned_videos(decisions, files) == {"IMG_1.MOV"}

    def test_video_is_kept_when_its_still_is_also_new(self):
        files = ["IMG_1.HEIC", "IMG_1.MOV"]
        decisions = {"IMG_1.HEIC": prune_imported.NEW, "IMG_1.MOV": prune_imported.NEW}
        assert prune_imported.orphaned_videos(decisions, files) == set()

    def test_a_present_video_does_not_orphan_a_missing_still(self):
        files = ["IMG_1.HEIC", "IMG_1.MOV"]
        decisions = {"IMG_1.HEIC": prune_imported.NEW, "IMG_1.MOV": prune_imported.PRESENT}
        assert prune_imported.orphaned_videos(decisions, files) == set()

    def test_standalone_video_is_never_orphaned(self):
        files = ["clip.mp4"]
        assert prune_imported.orphaned_videos({"clip.mp4": prune_imported.NEW}, files) == set()

    def test_pairing_does_not_cross_directories(self):
        """Same stem in two albums is two different photographs."""
        files = [os.path.join("a", "IMG_1.HEIC"), os.path.join("b", "IMG_1.MOV")]
        decisions = {
            os.path.join("a", "IMG_1.HEIC"): prune_imported.PRESENT,
            os.path.join("b", "IMG_1.MOV"): prune_imported.NEW,
        }
        assert prune_imported.orphaned_videos(decisions, files) == set()


class TestStillToDecide:
    def test_undecided_files_are_queued(self):
        assert prune_imported.still_to_decide(["a.jpg", "b.jpg"], {}) == ["a.jpg", "b.jpg"]

    def test_decided_files_are_skipped(self):
        decisions = {"a.jpg": prune_imported.PRESENT, "b.jpg": prune_imported.NEW}
        assert prune_imported.still_to_decide(["a.jpg", "b.jpg", "c.jpg"], decisions) == ["c.jpg"]

    def test_failures_are_retried(self):
        """A disk hiccup must not permanently strand a file as undecided."""
        decisions = {"a.jpg": prune_imported.FAILED, "b.jpg": prune_imported.NEW}
        assert prune_imported.still_to_decide(["a.jpg", "b.jpg"], decisions) == ["a.jpg"]

    def test_orphans_are_not_retried(self):
        decisions = {"a.mov": prune_imported.ORPHAN}
        assert prune_imported.still_to_decide(["a.mov"], decisions) == []

