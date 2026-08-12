from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "backend" / "scripts" / "preprocess_hvi.py"
spec = importlib.util.spec_from_file_location("preprocess_hvi", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)

RAW = Path("/mnt/data/urban-dossier-state/datasets/raw-expansion/hvi")
METADATA = Path("/mnt/data/urban-dossier-state/datasets/raw-expansion/_meta/4mhf-duep.json")


def test_real_snapshot_is_complete_and_preserves_official_quintiles():
    if not RAW.exists() or not METADATA.exists():
        pytest.skip("external HVI snapshot is not mounted")
    rows, build = module.build_rows(RAW, METADATA)
    assert len(rows) == 184
    assert len({row[0] for row in rows}) == 184
    assert build["population"]["hvi_distribution"] == {
        "1": 37, "2": 37, "3": 36, "4": 37, "5": 37,
    }
    assert {(raw, score) for _, raw, score in rows} == {
        (1, 100), (2, 75), (3, 50), (4, 25), (5, 0),
    }


def test_snapshot_hash_mismatch_fails_before_csv_parse(tmp_path):
    (tmp_path / module.SOURCE_CSV).write_text("zcta20,hvi\n10001,2\n")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "files": [{
            "file": module.SOURCE_CSV,
            "bytes": (tmp_path / module.SOURCE_CSV).stat().st_size,
            "sha256": "wrong",
        }]
    }))
    metadata = tmp_path / module.SOURCE_METADATA
    metadata.write_text("{}")
    with pytest.raises(ValueError, match="pinned manifest"):
        module.validate_snapshot(tmp_path, metadata)


def test_publish_emits_a_valid_atomic_pair(tmp_path):
    rows = [
        ("10001", 1, 100),
        ("10002", 2, 75),
        ("10003", 3, 50),
        ("10004", 4, 25),
        ("10005", 5, 0),
    ]
    build = {"inputs": {}, "source": {}, "population": {}, "normalization": {}}
    manifest = module.publish(rows, build, tmp_path)
    artifact = tmp_path / module.OUTPUT_RELPATH
    publication = tmp_path / module.MANIFEST_RELPATH
    assert artifact.exists() and publication.exists()
    assert manifest["artifact"]["sha256"] == module.sha256(artifact)
    assert manifest["artifact"]["columns"] == module.ARTIFACT_COLUMNS
    assert manifest["artifact"]["row_count"] == 5
