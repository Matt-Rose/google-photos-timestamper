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
