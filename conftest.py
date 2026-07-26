import json
import os
import shutil
import sys

import pytest

# main.py lives at the repo root as a flat module (no src/ package layout),
# so tests need the repo root on sys.path to `import main`.
sys.path.insert(0, os.path.dirname(__file__))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "test", "fixtures")


def _copy_fixture(name: str, dest_dir: str, dest_name: str) -> str:
    """Copy a fixture file into dest_dir (never mutate the committed original)."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, dest_name)
    shutil.copyfile(os.path.join(FIXTURES_DIR, name), dest)
    return dest


@pytest.fixture
def sample_jpg(tmp_path):
    def _make(dest_dir=None, dest_name="sample.jpg"):
        return _copy_fixture("sample.jpg", dest_dir or str(tmp_path), dest_name)

    return _make


@pytest.fixture
def sample_mov(tmp_path):
    def _make(dest_dir=None, dest_name="sample.mov"):
        return _copy_fixture("sample.mov", dest_dir or str(tmp_path), dest_name)

    return _make


def write_sidecar(path, timestamp, lat=None, lon=None, alt=None):
    """Write a minimal Google-Takeout-style JSON sidecar at `path`."""
    data = {"photoTakenTime": {"timestamp": str(timestamp)}}
    if lat is not None and lon is not None:
        data["geoDataExif"] = {
            "latitude": lat,
            "longitude": lon,
            "altitude": alt if alt is not None else 0.0,
        }
    with open(path, "w") as f:
        json.dump(data, f)
