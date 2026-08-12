from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from urban_dossier_backend.metrics import METHODOLOGY_VERSION
from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider
from urban_dossier_backend.publications import _validate_cached, ready_publication_valid


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    artifact = tmp_path / "score.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t (h3_r9 VARCHAR, raw_count DOUBLE, score INTEGER)")
    con.execute("INSERT INTO t VALUES ('a', 1.5, 80), ('b', 2.5, 20)")
    con.execute(f"COPY t TO '{artifact.as_posix()}' (FORMAT PARQUET)")
    con.close()
    manifest = tmp_path / "score.manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "1.0",
        "methodology_version": METHODOLOGY_VERSION,
        "artifact": {
            "path": artifact.name,
            "sha256": _sha(artifact),
            "size_bytes": artifact.stat().st_size,
            "row_count": 2,
            "columns": ["h3_r9", "raw_count", "score"],
        },
    }))
    return artifact, manifest


def test_exact_ready_publication_is_accepted(tmp_path):
    artifact, manifest = _fixture(tmp_path)
    assert ready_publication_valid(tmp_path, artifact.name, manifest.name)


def test_stale_methodology_fails_closed(tmp_path):
    artifact, manifest = _fixture(tmp_path)
    body = json.loads(manifest.read_text())
    body["methodology_version"] = "3.8.0"
    manifest.write_text(json.dumps(body))
    assert not ready_publication_valid(tmp_path, artifact.name, manifest.name)


def test_artifact_mutation_invalidates_cached_publication(tmp_path):
    artifact, manifest = _fixture(tmp_path)
    assert ready_publication_valid(tmp_path, artifact.name, manifest.name)
    artifact.write_bytes(artifact.read_bytes() + b"changed")
    assert not ready_publication_valid(tmp_path, artifact.name, manifest.name)
    _validate_cached.cache_clear()


def test_missing_manifest_fails_closed_but_legacy_table_remains_supported(tmp_path):
    artifact, _ = _fixture(tmp_path)
    assert not ready_publication_valid(tmp_path, artifact.name, "missing.json")
    assert ready_publication_valid(tmp_path, artifact.name, None)


def test_provider_dataset_coverage_uses_publication_gate(tmp_path):
    artifact, manifest = _fixture(tmp_path)
    environment = tmp_path / "environment"
    environment.mkdir()
    artifact.replace(environment / "nyccas_no_scores_h3.parquet")
    body = json.loads(manifest.read_text())
    body["artifact"]["path"] = "nyccas_no_scores_h3.parquet"
    body["artifact"]["sha256"] = _sha(environment / "nyccas_no_scores_h3.parquet")
    (environment / "nyccas_no.manifest.json").write_text(json.dumps(body))

    provider = DirectQueryDataProvider()
    provider.ready_dir = tmp_path
    assert provider._dataset_available("nyccas_no")

    body["methodology_version"] = "3.8.0"
    (environment / "nyccas_no.manifest.json").write_text(json.dumps(body))
    assert not provider._dataset_available("nyccas_no")


def test_nearest_location_prefers_ready_index_when_processed_is_absent(tmp_path):
    location_dir = tmp_path / "location"
    location_dir.mkdir()
    location_index = location_dir / "location_index.parquet"
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE locations (
            matched_address VARCHAR,
            borough VARCHAR,
            zip VARCHAR,
            latitude DOUBLE,
            longitude DOUBLE,
            canonical_location_id VARCHAR
        )
    """)
    con.execute(
        "INSERT INTO locations VALUES (?, ?, ?, ?, ?, ?)",
        ["338 5 AVENUE", "MANHATTAN", "10001", 40.7484514, -73.9857117, "test-1"],
    )
    con.execute(f"COPY locations TO '{location_index.as_posix()}' (FORMAT PARQUET)")

    provider = DirectQueryDataProvider()
    provider.ready_dir = tmp_path
    provider.processed_dir = None
    target = provider._nearest_location(con, 40.7484, -73.9857)
    con.close()

    assert target["zip"] == "10001"
    assert target["borough"] == "MANHATTAN"
    assert target["matched_address"] == "338 5 AVENUE"
