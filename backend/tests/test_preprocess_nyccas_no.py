from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from urban_dossier_backend.config import BOUNDARIES_DIR


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "backend" / "scripts" / "preprocess_nyccas_no.py"
spec = importlib.util.spec_from_file_location("preprocess_nyccas_no", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


RAW = Path("/mnt/data/urban-dossier-state/datasets/raw-expansion/nyccas")
BOUNDARY = BOUNDARIES_DIR / "nta_2020.geojson"


def test_raw_snapshot_hashes_match_manifest():
    if not RAW.exists():
        pytest.skip("external NYCCAS snapshot is not mounted")
    inputs = module.validate_raw_snapshot(RAW)
    assert inputs["archive"]["sha256"] == "7297bc43683d9d7476a8cc6469a58efd13512e6f07cc1ae1cc663bc93499bfdd"
    assert inputs["data_dictionary"]["sha256"] == "0fd57f8e7c95a7130366d70a5d4e96291100fc7c1bed5a4d3bd2fd7ffd9b4dbe"


def test_manifest_hash_mismatch_fails_before_raster_read(tmp_path):
    (tmp_path / module.SOURCE_ARCHIVE).write_bytes(b"archive")
    (tmp_path / module.SOURCE_DICTIONARY).write_bytes(b"dictionary")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "files": [
            {"file": module.SOURCE_ARCHIVE, "bytes": 7, "sha256": "wrong"},
            {"file": module.SOURCE_DICTIONARY, "bytes": 10, "sha256": "wrong"},
        ]
    }))
    with pytest.raises(ValueError, match="does not match manifest"):
        module.validate_raw_snapshot(tmp_path)


def test_real_snapshot_builds_complete_land_population():
    if not RAW.exists() or not BOUNDARY.exists():
        pytest.skip("external NYCCAS snapshot or boundary is not mounted")
    rows, build = module.build_rows(RAW, BOUNDARY)
    population = build["population"]
    assert population["land_cells"] == 7414
    assert population["scored_cells"] == 7413
    assert population["coverage_fraction"] > 0.999
    assert len(rows) == len({row[0] for row in rows})
    assert min(row[1] for row in rows) > 0
    assert max(row[1] for row in rows) < 50
    assert min(row[2] for row in rows) >= 0
    assert max(row[2] for row in rows) <= 100


def test_publish_emits_valid_atomic_pair(tmp_path):
    rows = [("cell-a", 5.0, 100), ("cell-b", 10.0, 0)]
    build = {
        "inputs": {},
        "boundary": {},
        "source_raster": {},
        "population": {"land_cells": 2, "scored_cells": 2, "coverage_fraction": 1.0},
        "raw_value_summary": {"min": 5.0, "median": 7.5, "max": 10.0},
    }
    manifest = module.publish(rows, build, tmp_path)
    artifact = tmp_path / module.OUTPUT_RELPATH
    publication = tmp_path / module.MANIFEST_RELPATH
    assert artifact.exists() and publication.exists()
    assert manifest["artifact"]["sha256"] == module.sha256(artifact)
    assert manifest["artifact"]["row_count"] == 2
    assert manifest["artifact"]["columns"] == module.ARTIFACT_COLUMNS
