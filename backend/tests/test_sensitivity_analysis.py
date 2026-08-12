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


def test_renormalize_matches_production_semantics():
    """One present metric out of two -> its score at full strength, exactly
    what `_weighted_score` does at runtime."""
    scores = np.array([[80.0, np.nan]])
    weights = np.array([0.6, 0.4])
    out = rsa.composite(scores, weights, None)
    assert out[0] == pytest.approx(80.0)


def test_impute_fills_with_citywide_mean_at_full_weight():
    scores = np.array([[80.0, np.nan]])
    weights = np.array([0.6, 0.4])
    means = np.array([70.0, 50.0])
    out = rsa.composite(scores, weights, means)
    assert out[0] == pytest.approx((80.0 * 0.6 + 50.0 * 0.4) / 1.0)


def test_cell_with_nothing_present_is_nan_not_zero():
    scores = np.array([[np.nan, np.nan]])
    out = rsa.composite(scores, np.array([0.5, 0.5]), None)
    assert np.isnan(out[0])


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
    # Both flagged metrics exist in real data, so both effects are measured.
    assert set(h["toggle_effects"]) == {"collision_transport", "311_sanitation"}
    assert len(frame) == per_cell.shape[0]
    # The production artifact was not touched.
    assert (tmp_path / "sensitivity_cells.parquet").exists()
