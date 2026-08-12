"""Score intervals reach the API, and their absence is honest.

Item 1.4's acceptance criterion was two-sided: the offline analysis exists
(covered in test_sensitivity_analysis) and the API serves its intervals.
These tests cover the serving half -- the lookup module directly, and the
analyze-point payload where real data allows.
"""
from __future__ import annotations

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
