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
import filter_batches  # noqa: E402
import import_survey  # noqa: E402
import transcode_vp9  # noqa: E402
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


class TestDescribeParticipant:
    def test_email_is_preferred_over_phone(self):
        assert sharing_status.describe_participant("a@b.com", "447700900123", 2) == \
            "a@b.com (accepted)"

    def test_phone_only_is_shown_with_a_plus(self):
        assert sharing_status.describe_participant(None, "447700900123", 2) == \
            "+447700900123 (accepted)"

    def test_an_already_formatted_number_does_not_get_a_second_plus(self):
        """Photos stores both '447700900123' and '+44 7700 900123'."""
        assert sharing_status.describe_participant(None, "+44 7700 900123", 1) == \
            "+44 7700 900123 (invited)"

    def test_invited_but_not_accepted(self):
        assert sharing_status.describe_participant("a@b.com", None, 1) == "a@b.com (invited)"

    def test_unknown_status_is_shown_raw_not_guessed(self):
        assert sharing_status.describe_participant("a@b.com", None, 7) == "a@b.com (status 7)"

    def test_no_identity_at_all(self):
        assert sharing_status.describe_participant(None, None, 1) == "unknown (invited)"


class TestAwaitingAcceptance:
    def test_accepted_participants_are_not_listed(self):
        assert sharing_status.awaiting_acceptance([("a@b.com (accepted)", 2)]) == []

    def test_invited_participants_are_listed(self):
        people = [("a@b.com (invited)", 1), ("c@d.com (accepted)", 2)]
        assert sharing_status.awaiting_acceptance(people) == ["a@b.com (invited)"]

    def test_unknown_status_counts_as_not_accepted(self):
        """Only status 2 is known to mean accepted; anything else must surface."""
        assert sharing_status.awaiting_acceptance([("a@b.com (status 7)", 7)]) == \
            ["a@b.com (status 7)"]

    def test_no_participants_is_empty(self):
        assert sharing_status.awaiting_acceptance([]) == []


class TestIntraSetDuplicates:
    def test_second_copy_of_the_same_content_is_dropped(self):
        rows = {
            "Photos from 2019/a.jpg": (prune_imported.NEW, "HASH1"),
            "album/a.jpg": (prune_imported.NEW, "HASH1"),
        }
        assert prune_imported.intra_set_duplicates(rows) == {"album/a.jpg"}

    def test_the_earliest_path_in_sort_order_is_kept(self):
        rows = {
            "z.jpg": (prune_imported.NEW, "HASH1"),
            "a.jpg": (prune_imported.NEW, "HASH1"),
            "m.jpg": (prune_imported.NEW, "HASH1"),
        }
        assert prune_imported.intra_set_duplicates(rows) == {"m.jpg", "z.jpg"}

    def test_different_content_is_not_deduplicated(self):
        rows = {
            "a.jpg": (prune_imported.NEW, "HASH1"),
            "b.jpg": (prune_imported.NEW, "HASH2"),
        }
        assert prune_imported.intra_set_duplicates(rows) == set()

    def test_files_already_resolved_against_the_library_are_ignored(self):
        """A present file is out of the set; it must not claim the hash slot."""
        rows = {
            "a.jpg": (prune_imported.PRESENT, "HASH1"),
            "b.jpg": (prune_imported.NEW, "HASH1"),
        }
        assert prune_imported.intra_set_duplicates(rows) == set()

    def test_files_without_a_hash_are_always_kept(self):
        """--no-video-hash leaves videos uncomparable; never guess."""
        rows = {
            "a.mov": (prune_imported.NEW, ""),
            "b.mov": (prune_imported.NEW, ""),
        }
        assert prune_imported.intra_set_duplicates(rows) == set()


class TestReadLedgerRows:
    def test_returns_decision_and_hash(self, tmp_path):
        ledger = tmp_path / "l.tsv"
        ledger.write_text("a.jpg\tnew\thash\tHASH1\nb.jpg\tpresent\thash\tUUID-1\n")
        assert prune_imported.read_ledger_rows(str(ledger)) == {
            "a.jpg": ("new", "HASH1"),
            "b.jpg": ("present", "UUID-1"),
        }

    def test_short_rows_yield_an_empty_hash(self, tmp_path):
        ledger = tmp_path / "l.tsv"
        ledger.write_text("a.mov\torphan-video\n")
        assert prune_imported.read_ledger_rows(str(ledger)) == {"a.mov": ("orphan-video", "")}


class TestOrphansAfterIntraSetDedup:
    def test_video_is_orphaned_when_its_still_is_an_in_set_duplicate(self):
        """The kept copy of the still pairs with its own video elsewhere."""
        files = ["album/IMG_1.HEIC", "album/IMG_1.MOV"]
        decisions = {
            "album/IMG_1.HEIC": prune_imported.INSET,
            "album/IMG_1.MOV": prune_imported.NEW,
        }
        assert prune_imported.orphaned_videos(decisions, files) == {"album/IMG_1.MOV"}


class TestConfirmOrphans:
    """The duration gate that stops real videos being dropped on a name clash."""

    def test_short_video_is_confirmed_as_a_live_photo_companion(self):
        assert prune_imported.confirm_orphans(
            {"a/IMG_1.MOV"}, "/root", duration=lambda p: 1.5) == {"a/IMG_1.MOV"}

    def test_long_video_is_kept_for_import(self):
        assert prune_imported.confirm_orphans(
            {"a/IMG_1.MOV"}, "/root", duration=lambda p: 25.0) == set()

    def test_the_boundary_is_inclusive(self):
        assert prune_imported.confirm_orphans(
            {"a/x.MOV"}, "/root", duration=lambda p: 4.0) == {"a/x.MOV"}
        assert prune_imported.confirm_orphans(
            {"a/x.MOV"}, "/root", duration=lambda p: 4.01) == set()

    def test_unmeasurable_file_is_kept_not_dropped(self):
        """Dropping a real video is silent; importing a duplicate is visible."""
        assert prune_imported.confirm_orphans(
            {"a/x.MOV"}, "/root", duration=lambda p: None) == set()

    def test_mixed_batch_splits_correctly(self):
        durations = {"/root/s.MOV": 2.0, "/root/l.MOV": 30.0, "/root/u.MOV": None}
        assert prune_imported.confirm_orphans(
            {"s.MOV", "l.MOV", "u.MOV"}, "/root",
            duration=lambda p: durations[p]) == {"s.MOV"}


class TestRelativeToTree:
    def test_strips_the_root(self):
        assert filter_batches.relative_to_tree("/a/b/Photos from 2019/x.jpg", "/a/b") == \
            "Photos from 2019/x.jpg"

    def test_tolerates_a_trailing_separator_on_the_root(self):
        assert filter_batches.relative_to_tree("/a/b/x.jpg", "/a/b/") == "x.jpg"

    def test_path_outside_the_tree_is_unmatched(self):
        assert filter_batches.relative_to_tree("/other/x.jpg", "/a/b") is None

    def test_a_similar_prefix_is_not_a_match(self):
        """/a/bb must not be treated as living under /a/b."""
        assert filter_batches.relative_to_tree("/a/bb/x.jpg", "/a/b") is None


class TestFilterLines:
    ROOT = "/tree"

    def test_keeps_new_and_drops_everything_else(self):
        decisions = {"a.jpg": "new", "b.jpg": "present",
                     "c.jpg": "dup-in-set", "d.mov": "orphan-video"}
        lines = [f"/tree/{n}\n" for n in ("a.jpg", "b.jpg", "c.jpg", "d.mov")]
        kept, dropped, unknown = filter_batches.filter_lines(lines, decisions, self.ROOT)
        assert kept == ["/tree/a.jpg"]
        assert [d for _, d in dropped] == ["present", "dup-in-set", "orphan-video"]
        assert unknown == []

    def test_unledgered_line_is_kept_and_reported(self):
        """Dropping a file the prune never examined would be guessing."""
        kept, dropped, unknown = filter_batches.filter_lines(
            ["/tree/x.MP\n"], {}, self.ROOT)
        assert kept == ["/tree/x.MP"]
        assert unknown == ["/tree/x.MP"]
        assert dropped == []

    def test_blank_lines_are_ignored(self):
        kept, _, _ = filter_batches.filter_lines(["\n", "  \n"], {}, self.ROOT)
        assert kept == []


class TestTrialSelection:
    def test_returns_everything_when_the_set_is_small(self):
        kept = ["/t/a.jpg", "/t/b.jpg"]
        assert filter_batches.trial_selection(kept, 500) == kept

    def test_never_splits_a_live_photo_pair(self):
        kept = [f"/t/IMG_{i}.HEIC" for i in range(50)] + \
               [f"/t/IMG_{i}.MOV" for i in range(50)]
        trial = filter_batches.trial_selection(kept, 10)
        stems = {filter_batches.group_stem(p) for p in trial}
        for stem in stems:
            assert f"{stem}.HEIC" in trial and f"{stem}.MOV" in trial

    def test_respects_the_requested_size(self):
        kept = [f"/t/f{i}.jpg" for i in range(1000)]
        assert len(filter_batches.trial_selection(kept, 100)) <= 100

    def test_spreads_across_the_set_rather_than_taking_a_prefix(self):
        kept = [f"/t/{y}/f{i}.jpg" for y in (2019, 2024) for i in range(100)]
        trial = filter_batches.trial_selection(kept, 20)
        years = {p.split("/")[2] for p in trial}
        assert years == {"2019", "2024"}


class TestEquivalentExtensions:
    def test_identical_is_equivalent(self):
        assert import_survey.equivalent("jpg", "jpg")

    def test_container_family_members_are_equivalent(self):
        """A .MOV holding MP4-branded ISO-BMFF is normal, not a mislabel."""
        assert import_survey.equivalent("mp4", "mov")
        assert import_survey.equivalent("jpg", "jpeg")
        assert import_survey.equivalent("heic", "heif")

    def test_genuine_mislabel_is_caught(self):
        """JPEG bytes under a .HEIC name — the real Google problem."""
        assert not import_survey.equivalent("jpg", "heic")
        assert not import_survey.equivalent("jpg", "png")

    def test_across_families_is_not_equivalent(self):
        assert not import_survey.equivalent("mp4", "jpg")


class TestDateProblem:
    def test_a_good_date_is_no_problem(self):
        assert import_survey.date_problem(["2023:05:08 12:48:14"]) is None

    def test_a_timezone_suffix_is_tolerated(self):
        assert import_survey.date_problem(["2016:12:20 16:15:08+00:00"]) is None

    def test_missing_dates_report_benign(self):
        assert import_survey.date_problem(["-", "", "-"]) == "no date"

    def test_out_of_range_year_is_flagged_as_unusable(self):
        assert import_survey.date_problem(["2090:72:01 00:00:00"]).startswith("unusable")

    def test_falls_back_to_a_later_usable_date(self):
        """One bad tag must not condemn a file that has a good one."""
        assert import_survey.date_problem(["0000:00:00 00:00:00",
                                           "2019:04:01 10:00:00"]) is None

    def test_zero_date_alone_is_unusable(self):
        assert import_survey.date_problem(["0000:00:00 00:00:00"]).startswith("unusable")


class TestBackupPath:
    """Originals must not collide: 237 files shared 114 basenames in one export."""

    def test_mirrors_the_tree_layout(self):
        assert transcode_vp9.backup_path(
            "/tree/Photos from 2024/IMG_1.MOV", "/tree", "/bk") == \
            os.path.join("/bk", "Photos from 2024", "IMG_1.MOV")

    def test_same_name_in_two_folders_gets_two_destinations(self):
        a = transcode_vp9.backup_path("/tree/2024/IMG_5408.MOV", "/tree", "/bk")
        b = transcode_vp9.backup_path("/tree/2025/IMG_5408.MOV", "/tree", "/bk")
        assert a != b

    def test_without_a_root_it_falls_back_to_the_basename(self):
        assert transcode_vp9.backup_path("/tree/a/IMG_1.MOV", "", "/bk") == \
            os.path.join("/bk", "IMG_1.MOV")


class TestEncodeCmd:
    def test_hdr_goes_to_ten_bit_hevc_with_the_apple_tag(self):
        cmd = transcode_vp9.encode_cmd("in.mov", "out.mov", is_hdr=True)
        assert "libx265" in cmd and "yuv420p10le" in cmd
        assert cmd[cmd.index("-tag:v") + 1] == "hvc1"

    def test_sdr_goes_to_eight_bit_h264(self):
        cmd = transcode_vp9.encode_cmd("in.mov", "out.mov", is_hdr=False)
        assert "libx264" in cmd and "yuv420p" in cmd

    def test_crf_is_applied_to_the_hdr_path(self):
        cmd = transcode_vp9.encode_cmd("in.mov", "out.mov", is_hdr=True, crf=26)
        assert cmd[cmd.index("-crf") + 1] == "26"

    def test_metadata_and_audio_are_carried_through(self):
        """Re-encoding the audio would be lossy for no reason."""
        cmd = transcode_vp9.encode_cmd("in.mov", "out.mov", is_hdr=True)
        assert cmd[cmd.index("-c:a") + 1] == "copy"
        assert "-map_metadata" in cmd


class TestAutoliveUnsafe:
    """Files that make osxphotos --auto-live abort the whole run."""

    def test_png_paired_with_a_video_is_unsafe(self):
        paths = ["a/IMG_1.PNG", "a/IMG_1.MOV"]
        assert filter_batches.autolive_unsafe(paths) == {"a/IMG_1.PNG"}

    def test_jpeg_paired_with_a_video_is_fine(self):
        paths = ["a/IMG_1.JPG", "a/IMG_1.MOV"]
        assert filter_batches.autolive_unsafe(paths) == set()

    def test_heic_paired_with_mp4_is_fine(self):
        paths = ["a/IMG_1.HEIC", "a/IMG_1.mp4"]
        assert filter_batches.autolive_unsafe(paths) == set()

    def test_a_video_outside_the_whitelist_is_unsafe(self):
        """makelive accepts only .mov/.mp4; an .m4v pair throws the same way."""
        paths = ["a/IMG_1.JPG", "a/IMG_1.m4v"]
        assert filter_batches.autolive_unsafe(paths) == {"a/IMG_1.m4v"}

    def test_lone_png_with_no_video_is_fine(self):
        """Nothing to pair with means makelive is never called."""
        assert filter_batches.autolive_unsafe(["a/IMG_1.PNG"]) == set()

    def test_png_and_video_in_different_folders_do_not_pair(self):
        paths = ["a/IMG_1.PNG", "b/IMG_1.MOV"]
        assert filter_batches.autolive_unsafe(paths) == set()

    def test_case_is_ignored(self):
        assert filter_batches.autolive_unsafe(["a/x.PnG", "a/x.MoV"]) == {"a/x.PnG"}


class TestClassifyWithoutSharedLibrary:
    def test_shared_library_check_can_be_skipped(self):
        """A library that never used the Shared Library: 0 in it must not mask the comparison."""
        assert sharing_status.classify(10, 0, 7, [], require_shared_library=False) == \
            sharing_status.DELETABLE
        assert sharing_status.classify(10, 0, 7, ["a.jpg"], require_shared_library=False) == \
            sharing_status.SHARED_INCOMPLETE
        assert sharing_status.classify(10, 0, None, [], require_shared_library=False) == \
            sharing_status.NO_SHARED_ALBUM

    def test_default_still_requires_shared_library(self):
        assert sharing_status.classify(10, 0, 7, []) == sharing_status.HAS_PRIVATE

