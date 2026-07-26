"""End-to-end test driving the real CLI entrypoint (main()) over a small
synthetic Takeout-style directory tree, to guard the thing that matters most:
every file that goes in comes out somewhere, intact, with nothing lost or
silently overwritten.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

from conftest import write_sidecar
import main as main_module

OLD_TIMESTAMP = int(datetime(2020, 1, 15, 10, 30, tzinfo=timezone.utc).timestamp())


def _all_file_contents(root):
    """Map of relative-path -> bytes for every real file under root."""
    contents = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            contents[os.path.relpath(full, root)] = open(full, "rb").read()
    return contents


def test_main_conserves_all_files_across_duplicate_named_albums(
    tmp_path, sample_jpg, monkeypatch
):
    input_dir = tmp_path / "input"

    # Two albums each containing a same-named photo -- a real Takeout shape,
    # and exactly the case where naive flat moves would collide/overwrite.
    album_a_photo = sample_jpg(str(input_dir / "AlbumA"), "photo.jpg")
    write_sidecar(album_a_photo + ".json", OLD_TIMESTAMP)

    album_b_photo = sample_jpg(str(input_dir / "AlbumB"), "photo.jpg")
    # No sidecar and no usable EXIF for this one -> should land in problems/.

    before = _all_file_contents(str(input_dir))
    assert len(before) == 3  # 2 photos + 1 sidecar

    monkeypatch.setattr("sys.argv", ["main.py", str(input_dir)])
    main_module.main()

    after_ready = _all_file_contents(str(input_dir / "ready"))
    after_problems = _all_file_contents(str(input_dir / "problems"))
    after_sidecars = _all_file_contents(str(input_dir / "sidecars"))

    # Nothing lost: every byte of every original file is accounted for
    # exactly once across the three output directories.
    all_after = {**after_ready, **after_problems, **after_sidecars}
    assert len(all_after) == 3

    # AlbumA/photo.jpg legitimately gets its bytes rewritten (EXIF was
    # written into it) -- assert it's still a valid, readable image with the
    # right date baked in, rather than byte-identical.
    ready_photo = str(input_dir / "ready" / "AlbumA" / "photo.jpg")
    result = subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", ready_photo],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)[0]["DateTimeOriginal"] == "2020:01:15 10:30:00"

    # Untouched files must be byte-identical to their originals.
    assert after_problems["AlbumB/photo.jpg"] == before["AlbumB/photo.jpg"]
    assert after_sidecars["AlbumA/photo.jpg.json"] == before["AlbumA/photo.jpg.json"]

    report = input_dir / "timestamper_report.md"
    assert report.exists()
    assert "**Total** | **2**" in report.read_text()

    # Emptied album directories get cleaned up; input_dir itself remains.
    assert not (input_dir / "AlbumA").exists()
    assert not (input_dir / "AlbumB").exists()
    assert input_dir.exists()
