"""The correlation analysis has to measure what it claims to measure.

Two layers. Synthetic fixtures pin the semantics -- above all the zero-fill:
a cell absent from a score table is a place where nothing was observed, and
treating it as missing (inner join) instead of zero changes the answer to the
double-counting question. The fixture for that is two series that agree
wherever both exist and disagree about where the zeros are; the inner join
calls them identical, the zero-filled frame does not.

The real-data layer runs only where the ready tables exist. It originally
pinned the byte-copied collision pair at exactly rho 1 -- that finding led to
the metric's removal in v3.8.0, so the pin flipped to asserting no duplicated
sources remain. The rodent/sanitation overlap stays pinned with a floor
deliberately below the measured 0.897, so a data refresh can move the number
without breaking the build while a real decoupling still fails loudly.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

_spec = importlib.util.spec_from_file_location(
    "analyze_metric_correlations",
    REPO_ROOT / "backend" / "scripts" / "analyze_metric_correlations.py",
)
amc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spec and amc)

duckdb = pytest.importorskip("duckdb")


def write_table(path: Path, rows: list[tuple[str, int, int]]) -> Path:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (h3_r9 VARCHAR, raw_count BIGINT, score BIGINT)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    return path


CELLS = [f"cell{i:03d}" for i in range(40)]


@pytest.fixture()
def con():
    return duckdb.connect()


def test_frame_is_the_union_not_the_intersection(tmp_path, con):
    a = write_table(tmp_path / "a.parquet", [(c, 1, 50) for c in CELLS[:30]])
    b = write_table(tmp_path / "b.parquet", [(c, 1, 50) for c in CELLS[20:]])
    frame = amc.build_frame(con, {"a": a, "b": b})
    assert len(frame) == 40
    assert frame == sorted(frame)


def test_absent_cells_are_zeros_not_missing(tmp_path, con):
    """The semantic core: absence must count against correlation.

    Both tables carry identical values on their shared cells, so an inner
    join would report rho = 1. But each observes a different half of the
    city, and over the full frame -- absences as zeros -- they anticorrelate.
    A regression that quietly switches the analysis to an inner join makes
    this fixture report near-perfect agreement and fails the assertion.
    """
    shared = [(c, 5, 50) for c in CELLS[18:22]]
    a = write_table(tmp_path / "a.parquet", [(c, 5, 50) for c in CELLS[:18]] + shared)
    b = write_table(tmp_path / "b.parquet", shared + [(c, 5, 50) for c in CELLS[22:]])
    tables = {"a": a, "b": b}
    frame = amc.build_frame(con, tables)
    counts = amc.zero_filled_counts(con, tables, frame)
    rho = amc.spearman_matrix(counts)
    assert rho[0, 1] < 0.0  # disjoint coverage reads as opposition, not agreement

    score_rho, n = amc.score_correlation_inner(con, a, b)
    assert n == 4
    # Not asserted equal to 1.0: scipy returns nan for a constant series,
    # which is exactly why the inner join is the wrong primary view.


def test_rate_absence_stays_missing_and_uses_pairwise_cells(tmp_path, con):
    """No inspection denominator is unknown, not a 0% failure rate."""
    rate = write_table(
        tmp_path / "rate.parquet",
        [(cell, value, value * 10) for value, cell in enumerate(CELLS[:10])],
    )
    count = write_table(
        tmp_path / "count.parquet",
        [(cell, value, value * 10) for value, cell in enumerate(CELLS)],
    )
    tables = {"rate": rate, "count": count}
    frame = amc.build_frame(con, tables)
    values = amc.raw_value_matrix(
        con,
        tables,
        frame,
        {"rate": False, "count": True},
    )

    assert np.isnan(values[0]).sum() == 30
    assert amc.spearman_matrix(values)[0, 1] == pytest.approx(1.0)



def test_identical_tables_measure_rho_one(tmp_path, con):
    rows = [(c, i * 3 % 17, 50) for i, c in enumerate(CELLS)]
    a = write_table(tmp_path / "a.parquet", rows)
    b = write_table(tmp_path / "b.parquet", rows)
    tables = {"a": a, "b": b}
    counts = amc.zero_filled_counts(con, tables, amc.build_frame(con, tables))
    rho = amc.spearman_matrix(counts)
    assert rho[0, 1] == pytest.approx(1.0)


def test_flag_pairs_applies_both_thresholds():
    rho = np.array(
        [
            [1.00, 0.95, 0.75, 0.10],
            [0.95, 1.00, 0.20, 0.05],
            [0.75, 0.20, 1.00, -0.72],
            [0.10, 0.05, -0.72, 1.00],
        ]
    )
    flagged = amc.flag_pairs(["a", "b", "c", "d"], rho)
    as_dict = {tuple(e["pair"]): e["level"] for e in flagged}
    assert as_dict[("a", "b")] == "collinear"
    assert as_dict[("a", "c")] == "high"
    assert as_dict[("c", "d")] == "high"  # negative correlation flags too
    assert ("a", "d") not in as_dict
    # strongest first
    assert flagged[0]["pair"] == ["a", "b"]


def test_registry_metrics_resolve_to_tables(tmp_path):
    """h3_metric_tables must skip absent files rather than invent entries."""
    found = amc.h3_metric_tables(tmp_path)
    assert found == {}


# --- real data, where present ----------------------------------------------

READY = Path("/mnt/data/Urban-Dossier/data/ready")

requires_ready = pytest.mark.skipif(
    not (READY / "safety" / "collisions_scores_h3.parquet").exists(),
    reason="ready score tables not present",
)


@requires_ready
def test_no_duplicated_source_pairs_remain_to_measure():
    """The rho = 1.000 pair this analysis caught was removed in v3.8.0.

    The registry no longer declares any duplicated source, so the report's
    duplicated_source section must be empty. The measurement that justified
    the removal is preserved in the git history of
    docs/methodology/metric-correlations.md.
    """
    report = amc.analyze(READY)
    assert [
        e for e in report["declared_relationships"] if e["kind"] == "duplicated_source"
    ] == []


@requires_ready
def test_the_rodent_sanitation_overlap_is_declared_measured_and_tamed():
    """The declared pair must be measured, and must stay below the old sickness.

    The count-era rodent metric co-moved with 311_sanitation at 0.897 --
    mostly shared volume. v3.9.0's inspection-anchored rate was accepted
    precisely because it broke that co-movement, so the ceiling here is the
    acceptance criterion: if this pair climbs back above the 0.7 high-
    correlation threshold, the construct has regressed to measuring volume
    twice. The floor only guards against a broken join measuring nothing.
    """
    report = amc.analyze(READY)
    declared = {
        tuple(sorted(e["pair"])): e["rho"]
        for e in report["declared_relationships"]
        if e["kind"] == "declared_overlap"
    }
    rho = declared[("311_sanitation", "rodent")]
    assert 0.1 <= rho < 0.7, rho


@requires_ready
def test_the_housing_sanitation_collinearity_is_declared_and_pinned():
    """The no-reweight decision depends on this overlap staying explicit."""
    report = amc.analyze(READY)
    declared = {
        tuple(sorted(e["pair"])): e["rho"]
        for e in report["declared_relationships"]
        if e["kind"] == "declared_overlap"
    }
    rho = declared[("311_sanitation", "housing_violations")]
    assert rho >= 0.85, rho


@requires_ready
def test_report_serialises_and_carries_the_matrix():
    import json

    report = amc.analyze(READY)
    json.dumps(report)
    n = len(report["h3_metrics"])
    assert len(report["matrix"]) == n
    assert all(len(row) == n for row in report["matrix"])
    assert report["method"]["frame_cells"] > 1000
