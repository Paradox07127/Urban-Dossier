"""The uncertainty analysis must be reproducible and mean what it says.

Synthetic fixtures pin the mechanics: ranking direction, the two missing-data
rules (renormalize matches production's `_weighted_score` semantics; impute
fills with citywide means at full weight), and byte-for-byte determinism under
a fixed seed -- an irreproducible uncertainty analysis is an anecdote, and the
published intervals are only defensible if the seed regenerates them.

The real-data smoke run uses 25 draws and writes its per-cell artifact to a
temp dir, never to `data/ready/analysis/` -- the production artifact there is
the 1,000-draw run and a test must not quietly replace it with a coarser one.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

_spec = importlib.util.spec_from_file_location(
    "run_sensitivity_analysis",
    REPO_ROOT / "backend" / "scripts" / "run_sensitivity_analysis.py",
)
rsa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spec and rsa)

duckdb = pytest.importorskip("duckdb")


# --- mechanics ---------------------------------------------------------------


def test_rank_one_is_best():
    ranks = rsa.rank_descending(np.array([30.0, 90.0, 60.0]))
    assert ranks.tolist() == [3.0, 1.0, 2.0]


def test_rank_leaves_nan_cells_unranked():
    ranks = rsa.rank_descending(np.array([30.0, np.nan, 60.0]))
    assert np.isnan(ranks[1])
    assert ranks[0] == 2.0 and ranks[2] == 1.0


# One category holding both metrics; the shape production actually aggregates.
TWO_METRIC_STRUCTURE = [("safety", 0.4, np.array([0, 1]), np.array([0.6, 0.4]))]


def test_category_renorm_then_round_matches_production_semantics():
    """One present metric out of two -> its score at full strength inside the
    category, then the category is the only overall contributor. Exactly the
    `_weighted_score` -> `_clamp` -> category-weighted path, which the old
    flat one-pass renormalisation did NOT reproduce (review: mean |delta|
    3.5 points against production)."""
    scores = np.array([[80.0, np.nan]])
    out = rsa.composite(scores, TWO_METRIC_STRUCTURE, None)
    assert out[0] == pytest.approx(80.0)


def test_composite_rounds_per_category_like_production():
    """77.5 within the category rounds to 78 BEFORE the overall stage --
    the integer staging the flat formula skipped."""
    scores = np.array([[75.0, 81.25]])
    out = rsa.composite(scores, TWO_METRIC_STRUCTURE, None)
    # 75*0.6 + 81.25*0.4 = 77.5 -> production _clamp rounds (banker's) to 78
    assert out[0] == pytest.approx(round(77.5))


def test_missing_weight_stays_inside_the_category():
    """Two categories: a metric missing in one must NOT leak its weight to
    the other category -- the exact defect of the flat renormalisation."""
    structure = [
        ("safety", 0.4, np.array([0, 1]), np.array([0.5, 0.5])),
        ("transit", 0.3, np.array([2]), np.array([1.0])),
    ]
    scores = np.array([[100.0, np.nan, 0.0]])
    out = rsa.composite(scores, structure, None)
    # safety = 100 (renorm inside category), transit = 0
    # overall = (0.4*100 + 0.3*0) / 0.7 = 57.14 -> 57
    assert out[0] == pytest.approx(57.0)
    # The flat formula would have given (0.4*0.5*100)/(0.4*0.5+0.3) = 40.


def test_impute_fills_with_citywide_mean_at_full_weight():
    scores = np.array([[80.0, np.nan]])
    means = np.array([70.0, 50.0])
    out = rsa.composite(scores, TWO_METRIC_STRUCTURE, means)
    assert out[0] == pytest.approx(round(80.0 * 0.6 + 50.0 * 0.4))


def test_cell_with_nothing_present_is_nan_not_zero():
    scores = np.array([[np.nan, np.nan]])
    out = rsa.composite(scores, TWO_METRIC_STRUCTURE, None)
    assert np.isnan(out[0])


def test_toggle_zeroes_a_metric_via_multiplier():
    scores = np.array([[80.0, 20.0]])
    multiplier = np.array([1.0, 0.0])
    out = rsa.composite(scores, TWO_METRIC_STRUCTURE, None, multiplier)
    assert out[0] == pytest.approx(80.0)


def test_zip_lookup_repeats_native_zip_without_changing_its_declared_grain(tmp_path):
    import h3

    cell = h3.latlng_to_cell(40.7484, -73.9857, 9)
    location = tmp_path / rsa.LOCATION_INDEX_RELPATH
    location.parent.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE locations (latitude DOUBLE, longitude DOUBLE, zip VARCHAR)"
    )
    con.execute("INSERT INTO locations VALUES (40.7484514, -73.9857117, '10001')")
    con.execute(f"COPY locations TO '{location.as_posix()}' (FORMAT PARQUET)")
    assert rsa.cell_zip_lookup(con, tmp_path, [cell]) == ["10001"]
    con.close()


# --- determinism on a synthetic ready root -----------------------------------


def make_ready_root(tmp_path: Path) -> Path:
    """A miniature ready layer: three registered H3 metrics, 30 cells."""
    rng = np.random.default_rng(42)
    con = duckdb.connect()
    cells = [f"89x{i:04d}" for i in range(30)]
    tables = {
        "safety/collisions_scores_h3.parquet": 1.0,
        "safety/rodent_scores_h3.parquet": 0.7,
        "transit/subway_scores_h3.parquet": 0.1,
    }
    for rel, density in tables.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            (c, int(rng.integers(0, 50)), int(rng.integers(0, 101)))
            for c in cells
            if rng.random() < density
        ]
        con.execute("CREATE OR REPLACE TABLE t (h3_r9 VARCHAR, raw_count BIGINT, score BIGINT)")
        con.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
        con.execute(f"COPY t TO '{path.as_posix()}' (FORMAT PARQUET)")
    return tmp_path


def test_same_seed_reproduces_the_summary_exactly(tmp_path):
    root = make_ready_root(tmp_path)
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    summary_a, cells_a, _ = rsa.run(root, draws=60, seed=7, cell_output_dir=out_a)
    summary_b, cells_b, _ = rsa.run(root, draws=60, seed=7, cell_output_dir=out_b)
    assert json.dumps(summary_a, sort_keys=True) == json.dumps(summary_b, sort_keys=True)
    assert np.array_equal(cells_a, cells_b, equal_nan=True)


def test_different_seed_actually_changes_the_draws(tmp_path):
    root = make_ready_root(tmp_path)
    summary_a, *_ = rsa.run(root, draws=60, seed=7, cell_output_dir=tmp_path / "a")
    summary_b, *_ = rsa.run(root, draws=60, seed=8, cell_output_dir=tmp_path / "b")
    assert json.dumps(summary_a["headline"]) != json.dumps(summary_b["headline"])


def test_fixture_without_toggle_metrics_runs_with_empty_toggle_effects(tmp_path):
    """Neither flagged metric exists in the mini fixture; the design must
    degrade to an empty toggle section rather than fail."""
    root = make_ready_root(tmp_path)
    summary, *_ = rsa.run(root, draws=40, seed=3, cell_output_dir=tmp_path / "o")
    assert summary["design"]["toggles"] == []
    assert summary["headline"]["toggle_effects"] == {}
    assert summary["cells"] == 30


def test_per_cell_artifact_is_written_where_told(tmp_path):
    root = make_ready_root(tmp_path)
    out = tmp_path / "elsewhere"
    rsa.run(root, draws=20, seed=1, cell_output_dir=out)
    assert (out / "sensitivity_cells.parquet").exists()
    manifest = json.loads((out / "sensitivity_cells.manifest.json").read_text())
    artifact = manifest["artifact"]
    assert manifest["schema_version"] == "1.0"
    assert manifest["methodology_version"] == rsa.METHODOLOGY_VERSION
    assert manifest["draws"] == 20
    assert artifact["row_count"] == 30
    assert artifact["columns"] == list(rsa.ARTIFACT_COLUMNS)
    assert artifact["sha256"] == rsa._sha256(out / "sensitivity_cells.parquet")
    assert set(manifest["input_score_tables"]) == {"collision", "subway"}
    assert not (root / "analysis").exists()


# --- real data, where present ------------------------------------------------

READY = Path("/mnt/data/Urban-Dossier/data/ready")

requires_ready = pytest.mark.skipif(
    not (READY / "safety" / "collisions_scores_h3.parquet").exists(),
    reason="ready score tables not present",
)


@requires_ready
def test_real_data_smoke(tmp_path):
    summary, per_cell, frame = rsa.run(READY, draws=25, seed=1, cell_output_dir=tmp_path)
    h = summary["headline"]
    assert summary["cells"] > 1000
    assert h["median_interval_width"] > 0
    assert h["median_interval_width_production_norm"] > 0
    # collision_transport left the registry in v3.8.0, so the only remaining
    # flagged toggle is the sanitation evidence source.
    assert set(h["toggle_effects"]) == {"311_sanitation"}
    assert {"ems_response", "fire_response", "parks_access", "heat_vulnerability"} <= set(
        summary["metrics"]
    )
    assert len(frame) == per_cell.shape[0]
    # The production artifact was not touched.
    assert (tmp_path / "sensitivity_cells.parquet").exists()
    manifest = json.loads((tmp_path / "sensitivity_cells.manifest.json").read_text())
    assert rsa.LOCATION_INDEX_INPUT_ID in manifest["input_score_tables"]
