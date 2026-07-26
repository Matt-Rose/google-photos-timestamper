import os

from main import FileResult, Outcome, _remove_empty_dirs, move_file, organise_files


def _write(path: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def test_move_file_with_base_dir_mirrors_relative_path(tmp_path):
    src = tmp_path / "input" / "Album" / "a.jpg"
    _write(str(src), b"content")
    dest_root = tmp_path / "ready"

    move_file(str(src), str(dest_root), base_dir=str(tmp_path / "input"))

    dest = dest_root / "Album" / "a.jpg"
    assert dest.read_bytes() == b"content"
    assert not src.exists()


def test_move_file_without_base_dir_avoids_collision(tmp_path):
    dest_root = tmp_path / "problems"
    existing = dest_root / "a.jpg"
    _write(str(existing), b"first")

    src = tmp_path / "a.jpg"
    _write(str(src), b"second")

    move_file(str(src), str(dest_root))

    # Neither file was lost or overwritten.
    assert existing.read_bytes() == b"first"
    assert (dest_root / "a_1.jpg").read_bytes() == b"second"


def test_remove_empty_dirs_removes_only_truly_empty_album_dirs(tmp_path):
    input_dir = tmp_path / "input"
    empty_album = input_dir / "EmptyAlbum"
    full_album = input_dir / "FullAlbum"
    os.makedirs(empty_album)
    os.makedirs(full_album)
    _write(str(full_album / "keep.jpg"), b"x")
    # A directory that only contains .DS_Store should still count as empty.
    ds_store_only = input_dir / "DsStoreOnly"
    os.makedirs(ds_store_only)
    (ds_store_only / ".DS_Store").write_bytes(b"")
    # OUTPUT_DIRS themselves must never be removed even if empty.
    ready_dir = input_dir / "ready"
    os.makedirs(ready_dir)

    _remove_empty_dirs(str(input_dir))

    assert not empty_album.exists()
    assert not ds_store_only.exists()
    assert full_album.exists()
    assert (full_album / "keep.jpg").exists()
    assert ready_dir.exists()
    assert input_dir.exists()


def test_organise_files_conserves_every_file_and_content(tmp_path):
    """The core no-lost-no-corrupted-photos guarantee: every input file must
    land in exactly one of ready/sidecars/problems with its content intact."""
    input_dir = tmp_path / "input"

    good_photo = input_dir / "AlbumA" / "photo.jpg"
    good_sidecar = input_dir / "AlbumA" / "photo.jpg.json"
    bad_photo = input_dir / "AlbumB" / "photo.jpg"  # same basename, different album
    orphan_photo = input_dir / "AlbumC" / "orphan.jpg"

    _write(str(good_photo), b"good-photo-bytes")
    _write(str(good_sidecar), b"{}")
    _write(str(bad_photo), b"bad-photo-bytes")
    _write(str(orphan_photo), b"orphan-photo-bytes")

    results = [
        FileResult(str(good_photo), Outcome.UPDATED, json_path=str(good_sidecar)),
        FileResult(str(bad_photo), Outcome.NO_JSON),
        FileResult(str(orphan_photo), Outcome.ERROR, error="boom"),
    ]

    report_path = os.path.join(str(input_dir), "timestamper_report.md")
    with open(report_path, "w") as f:
        f.write("report")

    organise_files(str(input_dir), results, report_path)

    ready = input_dir / "ready" / "AlbumA" / "photo.jpg"
    sidecar = input_dir / "sidecars" / "AlbumA" / "photo.jpg.json"
    problem_bad = input_dir / "problems" / "AlbumB" / "photo.jpg"
    problem_orphan = input_dir / "problems" / "AlbumC" / "orphan.jpg"

    assert ready.read_bytes() == b"good-photo-bytes"
    assert sidecar.exists()
    assert problem_bad.read_bytes() == b"bad-photo-bytes"
    assert problem_orphan.read_bytes() == b"orphan-photo-bytes"

    # Same basename in two different albums must never collide/overwrite.
    assert ready.read_bytes() != problem_bad.read_bytes()

    # The report file itself must be left alone, not swept into problems/.
    assert os.path.exists(report_path)
