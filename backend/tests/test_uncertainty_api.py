"""Score intervals reach the API, and their absence is honest.

Item 1.4's acceptance criterion was two-sided: the offline analysis exists
(covered in test_sensitivity_analysis) and the API serves its intervals.
These tests cover the serving half -- the lookup module directly, and the
analyze-point payload where real data allows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend import uncertainty
from urban_dossier_backend.config import READY_DATA_DIR
from urban_dossier_backend.metrics import METHODOLOGY_VERSION

CELLS = READY_DATA_DIR / "analysis" / "sensitivity_cells.parquet"

requires_cells = pytest.mark.skipif(
    not CELLS.exists(),
    reason="sensitivity_cells.parquet not generated; run run_sensitivity_analysis.py",
)


@pytest.fixture(autouse=True)
def fresh_cache():
    uncertainty.reset_cache()
    yield
    uncertainty.reset_cache()


@pytest.mark.parametrize(
    ("score_range", "label"),
    [
        ([5, 19.9], "Very low"),
        ([19, 21], "Very low–Low"),
        ([42, 58], "Middle"),
        ([55, 82], "Middle–Very high"),
        ([100, 100], "Very high"),
    ],
)
def test_public_tier_is_server_owned_and_interval_driven(score_range, label):
    tier = uncertainty.public_tier(score_range)
    assert tier["label"] == label
    assert tier["basis"] == "production_normalization_95pct_interval"
    assert tier["scale"] == "fixed_20_point_score_bands"


def _write_publication_fixture(tmp_path: Path, methodology_version: str) -> tuple[Path, Path]:
    import duckdb
    import h3
    import hashlib
    cells_path = tmp_path / "sensitivity_cells.parquet"
    cell = h3.latlng_to_cell(40.7282, -73.9942, 9)
    duckdb.connect().execute(
        """
        CREATE TABLE t AS SELECT
          ?::VARCHAR AS h3_r9, 52.0::DOUBLE AS nominal, 51.0::DOUBLE AS median,
          20.0::DOUBLE AS lo95, 80.0::DOUBLE AS hi95,
          44.0::DOUBLE AS lo95_prodnorm, 63.0::DOUBLE AS hi95_prodnorm,
          1.0::DOUBLE AS rank_nominal, 1.0::DOUBLE AS rank_median,
          1.0::DOUBLE AS rank_p5, 1.0::DOUBLE AS rank_p95
        """,
        [cell],
    ).execute(f"COPY t TO '{cells_path.as_posix()}' (FORMAT PARQUET)")
    digest = hashlib.sha256(cells_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "sensitivity_cells.manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": "1.0",
        "methodology_version": methodology_version,
        "generated": "2026-08-12",
        "seed": 7,
        "draws": 1000,
        "artifact": {
            "filename": cells_path.name,
            "sha256": digest,
            "size_bytes": cells_path.stat().st_size,
            "row_count": 1,
            "columns": list(uncertainty._EXPECTED_COLUMNS),
        },
        "input_score_tables": {},
    }))
    return cells_path, manifest_path


def test_stale_publication_fails_closed_then_file_change_reloads(monkeypatch, tmp_path):
    cells_path, manifest_path = _write_publication_fixture(tmp_path, "0.0.0")
    monkeypatch.setattr(uncertainty, "_CELLS_PATH", cells_path)
    monkeypatch.setattr(uncertainty, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(uncertainty, "_INPUT_PATHS", {})
    uncertainty.reset_cache()
    assert uncertainty.score_uncertainty(40.7282, -73.9942) is None

    body = json.loads(manifest_path.read_text())
    body["methodology_version"] = METHODOLOGY_VERSION
    manifest_path.write_text(json.dumps(body))
    out = uncertainty.score_uncertainty(40.7282, -73.9942)
    assert out is not None
    assert out["draws"] == 1000
    assert out["artifact_version"] == body["artifact"]["sha256"]
    assert out["public_tier"]["label"] == "Middle–High"


def test_artifact_hash_mismatch_fails_closed(monkeypatch, tmp_path):
    cells_path, manifest_path = _write_publication_fixture(tmp_path, METHODOLOGY_VERSION)
    body = json.loads(manifest_path.read_text())
    body["artifact"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(body))
    monkeypatch.setattr(uncertainty, "_CELLS_PATH", cells_path)
    monkeypatch.setattr(uncertainty, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(uncertainty, "_INPUT_PATHS", {})
    uncertainty.reset_cache()
    assert uncertainty.score_uncertainty(40.7282, -73.9942) is None


def test_input_snapshot_change_invalidates_a_live_cache(monkeypatch, tmp_path):
    import hashlib

    cells_path, manifest_path = _write_publication_fixture(tmp_path, METHODOLOGY_VERSION)
    input_path = tmp_path / "safety" / "metric_scores_h3.parquet"
    input_path.parent.mkdir()
    input_path.write_bytes(b"published-a")
    body = json.loads(manifest_path.read_text())
    body["input_score_tables"] = {
        "metric": {
            "path": "safety/metric_scores_h3.parquet",
            "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            "size_bytes": input_path.stat().st_size,
        }
    }
    manifest_path.write_text(json.dumps(body))
    monkeypatch.setattr(uncertainty, "READY_DATA_DIR", tmp_path)
    monkeypatch.setattr(uncertainty, "_CELLS_PATH", cells_path)
    monkeypatch.setattr(uncertainty, "_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(uncertainty, "_INPUT_PATHS", {"metric": input_path})
    uncertainty.reset_cache()
    assert uncertainty.score_uncertainty(40.7282, -73.9942) is not None

    input_path.write_bytes(b"published-b")
    assert uncertainty.score_uncertainty(40.7282, -73.9942) is None


@requires_cells
def test_a_manhattan_point_gets_both_intervals():
    out = uncertainty.score_uncertainty(40.7282, -73.9942)
    assert out is not None
    lo, hi = out["score_range"]
    lo_all, hi_all = out["score_range_all_methods"]
    assert lo < hi
    assert lo_all < hi_all
    # Deliberately NOT asserted: that the all-methods interval contains the
    # within-method one. Percentiles of a subset of draws are not bounded by
    # percentiles of the full set -- when the alternative normalizations pull
    # a cell's upper tail down, the production-norm 97.5th can sit slightly
    # above the all-draws 97.5th (observed: 55.9 vs 55.5). The honest
    # relationship is about typical width, checked below across many cells.
    assert (hi_all - lo_all) > 0 and (hi - lo) > 0
    assert out["methodology_version"] == METHODOLOGY_VERSION
    assert out["grain"] == "h3_r9_cell"
    distribution = out["distribution"]
    assert distribution["grain"] == "h3_r9_analysis_cells"
    assert sum(item["count"] for item in distribution["bins"]) == distribution[
        "population_n"
    ]
    assert distribution["marker_score"] == out["nominal_score"]
    assert distribution["marker_percentile"] == out["nominal_percentile"]
    assert 0 <= out["nominal_percentile"] <= 1


@requires_cells
def test_open_water_has_no_interval_rather_than_a_fake_one():
    out = uncertainty.score_uncertainty(40.50, -73.70)  # Atlantic, off Rockaway
    assert out is None


@requires_cells
def test_all_methods_intervals_are_typically_wider():
    """Width, not containment, is the defensible claim -- checked in bulk."""
    import duckdb

    rows = duckdb.connect().execute(
        f"""
        SELECT avg(hi95 - lo95), avg(hi95_prodnorm - lo95_prodnorm)
        FROM read_parquet('{CELLS.as_posix()}')
        """
    ).fetchone()
    assert rows[0] > rows[1]


@requires_cells
def test_rank_range_is_a_share_of_the_city():
    out = uncertainty.score_uncertainty(40.7282, -73.9942)
    lo, hi = out["rank_range_share"]
    assert 0.0 <= lo <= hi <= 1.0


def test_missing_artifact_yields_none_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(uncertainty, "_CELLS_PATH", tmp_path / "absent.parquet")
    uncertainty.reset_cache()
    assert uncertainty.score_uncertainty(40.7282, -73.9942) is None


@requires_cells
def test_analyze_point_payload_carries_score_uncertainty():
    """The field the acceptance criterion names, on the real service path."""
    from urban_dossier_backend.service import preview_point

    payload = preview_point(
        latitude=40.7282,
        longitude=-73.9942,
        radius_m=500,
        priority_order=["safety", "transit", "amenities"],
        time_window_days=365,
    )
    su = payload["score_uncertainty"]
    assert su is not None
    assert su["score_range"][0] < su["score_range"][1]
