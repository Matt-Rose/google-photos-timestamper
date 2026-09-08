"""Unit tests for the pure logic in tools/photos_import.py.

The AppleScript and process-control paths need a real Photos.app and are
exercised by running the tool; what is testable in isolation is the chunking
(which must never split a Live Photo pair) and the daemon CPU summing.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import photos_import  # noqa: E402


def test_chunk_keeps_live_photo_pair_together():
    """A still and its video share a stem and must land in the same chunk."""
    files = [f"/d/IMG_{n:04d}.HEIC" for n in range(6)]
    files += [f"/d/IMG_{n:04d}.MP4" for n in range(6)]
    chunks = photos_import.chunk_files(files, chunk_size=5)

    for stem in {os.path.splitext(os.path.basename(f))[0] for f in files}:
        holding = [i for i, c in enumerate(chunks) if any(stem in os.path.basename(f) for f in c)]
        assert len(holding) == 1, f"{stem} was split across chunks {holding}"


def test_chunk_may_exceed_size_rather_than_split_a_group():
    """Correctness beats the size cap: a group is never broken up."""
    files = ["/d/A.HEIC", "/d/A.MP4", "/d/A.AAE"]
    assert photos_import.chunk_files(files, chunk_size=1) == [files]


def test_chunk_covers_every_file_exactly_once():
    files = [f"/d/f{n}.jpg" for n in range(23)]
    chunks = photos_import.chunk_files(files, chunk_size=5)
    flat = [f for c in chunks for f in c]
    assert sorted(flat) == sorted(files)
    assert len(flat) == len(set(flat))


def test_chunk_empty_input():
    assert photos_import.chunk_files([], chunk_size=10) == []


def test_analysis_cpu_sums_only_the_named_daemons(monkeypatch):
    ps_output = (
        "%CPU COMM\n"
        " 12.5 /usr/libexec/mediaanalysisd\n"
        "  7.5 /usr/libexec/photoanalysisd\n"
        "  1.0 cloudphotod\n"
        " 99.9 /Applications/Firefox.app/Contents/MacOS/firefox\n"
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=ps_output, stderr="")

    monkeypatch.setattr(photos_import.subprocess, "run", fake_run)
    assert photos_import.analysis_cpu() == pytest.approx(21.0)


def test_analysis_cpu_ignores_unparseable_lines(monkeypatch):
    ps_output = "%CPU COMM\nnotanumber mediaanalysisd\n 5.0 cloudphotod\n"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=ps_output, stderr="")

    monkeypatch.setattr(photos_import.subprocess, "run", fake_run)
    assert photos_import.analysis_cpu() == pytest.approx(5.0)


def test_photos_pid_filters_by_current_user(monkeypatch):
    """Another account's Photos must never be returned.

    With fast user switching, a second account's Photos.app matches the same
    process pattern. Without a -u filter, restart_photos() would force-quit
    someone else's session.
    """
    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="4242\n", stderr="")

    monkeypatch.setattr(photos_import.subprocess, "run", fake_run)
    assert photos_import.photos_pid() == "4242"
    assert "-u" in seen["cmd"], "pgrep must filter by user"
    assert str(os.getuid()) in seen["cmd"]


def test_photos_pid_none_when_not_running(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(photos_import.subprocess, "run", fake_run)
    assert photos_import.photos_pid() is None


class TestFirstError:
    """A failure must be identifiable from the console line alone.

    A TCC denial on the library volume fails every chunk identically and was
    misread as a hung Photos, costing eight needless restarts.
    """

    def test_extracts_permission_error(self):
        output = (
            "Importing file 1/20\n"
            'OSError: Error Domain=NSCocoaErrorDomain Code=513 "Photos.sqlite" '
            "couldn't be copied because you don't have permission\n"
        )
        assert "OSError" in photos_import.first_error(output)

    def test_extracts_applescript_error(self):
        output = "blah\nAppleScriptError: run_script 'albumAdd' failed: User cancelled. (-128)\n"
        assert "albumAdd" in photos_import.first_error(output)

    def test_returns_none_when_nothing_recognisable(self):
        assert photos_import.first_error("just some chatter\nmore chatter\n") is None

    def test_truncates_very_long_lines(self):
        assert len(photos_import.first_error("OSError: " + "x" * 500)) <= 160


def test_import_chunk_returns_output_not_just_a_bool(monkeypatch):
    """Discarding osxphotos' output is what made the TCC failure undiagnosable."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="out", stderr="OSError: nope")

    monkeypatch.setattr(photos_import.subprocess, "run", fake_run)
    ok, output = photos_import.import_chunk(["/a.jpg"], "Album", "osxphotos")
    assert ok is False
    assert "OSError: nope" in output
