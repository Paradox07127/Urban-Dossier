"""Uncertainty analysis for the composite score -- EXPANSION_PLAN item 1.4.

The scores are published as single integers, but they rest on assumptions
nobody has stress-tested: expert weights, one normalization method, a
renormalize-over-present missing-data rule, and one remaining metric-overlap
toggle identified by the correlation report (item 1.3). The OECD/JRC
handbook calls the uncertainty and sensitivity step the line between an
institutional-grade indicator and an amateur one; this script is that step,
sized to the sources collected for it.

Design (each element traceable to a collected source, see
/mnt/data/urban-dossier-state/reference/methodology/RECOMMENDATIONS.md):

* 1,000 Monte Carlo draws, intervals as the 2.5th/97.5th percentiles of the
  draws -- CDC PLACES' published convention for its tract estimates.
* Per draw, sampled simultaneously:
    - sub-metric weights, each multiplied by U(0.75, 1.25) -- COINr's
      documented noisy-weights design with NoiseFactor 0.25;
    - normalization method: the published empirical-percentile scores, or
      min-max, or z-score (both recomputed from raw values, direction-aware)
      -- the handbook's method-substitution test;
    - inclusion of `311_sanitation` (Bernoulli 1/2) -- the retained
      complaint/inspection construct toggle. The duplicated
      `collision_transport` metric was removed in v3.8 and is no longer a
      live assumption; housing is not toggled because its overall weight is
      zero;
    - missing-data rule: renormalize over present metrics (production
      behaviour) or impute absent sub-scores with the metric's citywide mean
      -- the handbook's implicit-vs-explicit imputation comparison.
* Published per cell: nominal score, median, 95% interval, nominal rank,
  rank range (5th/95th percentile of ranks across draws) -- COINr's
  RankStats shape. Published overall: mean absolute rank shift (R_bar_S,
  Saisana 2005) and the isolated effect of each toggle.

Unit of analysis: the H3 r9 cells of the correlation frame, scored per cell
from the ready tables. This is deliberately the cell-level composite, not the
radius-aggregated point analysis -- the assumptions under test enter at
aggregation, and cell level isolates them from the geometry of grid_disk.

Scope limits, stated rather than hidden:
* The three ZIP-grain metrics are held out entirely (score and weight), the
  same way production handles a missing ZIP: renormalization over what is
  present. Mixing grains would need an allocation rule; item 1.5's plan is
  disclosure, not downscaling.
* `restaurant_context`'s inspection-quality adjustment exists only in the
  published percentile scores; the min-max and z branches rebuild from the
  score tables' canonical `raw_count` field. Despite that legacy field name,
  the rodent table stores its EB-shrunk inspection-positive rate there. The
  report notes the restaurant asymmetry.
* Absence from a sparse risk table (e.g. `aep`, 586 cells) is treated as
  missing, matching production. Whether "no AEP building here" should instead
  count as good news is a real open question -- it is surfaced in the report,
  not silently answered by either choice.

Deterministic under --seed (default 20260811). Offline batch, per
EXPANSION_PLAN 1.4: nothing here runs in a request path.

Usage:
    python backend/scripts/run_sensitivity_analysis.py [--draws 1000]
        [--ready-root PATH] [--seed N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from urban_dossier_backend.metrics import (  # noqa: E402
    CATEGORIES_BY_ID,
    METRICS,
    Direction,
    METHODOLOGY_VERSION,
)
from urban_dossier_backend.publications import ready_publication_valid  # noqa: E402

WEIGHT_NOISE = 0.25          # COINr get_noisy_weights, NoiseFactor 0.25
INTERVAL = (2.5, 97.5)       # CDC PLACES convention
# Metrics whose inclusion is itself an assumption worth perturbing. Absent ids
# are skipped gracefully, so this list can name metrics that later leave the
# registry -- collision_transport did exactly that in v3.8.0, resolving the
# question its toggle was here to quantify.
TOGGLE_METRICS = ("311_sanitation",)
NORMALIZATIONS = ("percentile", "minmax", "zscore")
PUBLICATION_SCHEMA_VERSION = "1.0"
ARTIFACT_COLUMNS = (
    "h3_r9", "nominal", "median", "lo95", "hi95",
    "lo95_prodnorm", "hi95_prodnorm",
    "rank_nominal", "rank_median", "rank_p5", "rank_p95",
)


def h3_metrics() -> list:
    return [m for m in METRICS if m.spatial_grain.value == "h3_r9"]


def load_matrices(ready_root: Path) -> tuple[list[str], list[str], np.ndarray, dict[str, np.ndarray]]:
    """Frame cells, metric ids, nominal weights, and one score matrix per
    normalization. Score matrices are cells x metrics with NaN where absent."""
    con = duckdb.connect()
    metrics = [
        metric
        for metric in h3_metrics()
        if ready_publication_valid(
            ready_root,
            metric.score_table,
            metric.publication_manifest,
        )
    ]
    positive_weight_metrics = [
        metric
        for metric in metrics
        if CATEGORIES_BY_ID[metric.category].weight_in_overall > 0
    ]
    union = " UNION ".join(
        f"SELECT h3_r9 FROM read_parquet('{(ready_root / m.score_table).as_posix()}')"
        for m in positive_weight_metrics
    )
    frame = sorted(r[0] for r in con.execute(union).fetchall())
    index = {c: i for i, c in enumerate(frame)}

    n_cells, n_metrics = len(frame), len(metrics)
    published = np.full((n_cells, n_metrics), np.nan)
    raw_values = np.full((n_cells, n_metrics), np.nan)
    for j, metric in enumerate(metrics):
        rows = con.execute(
            f"SELECT h3_r9, score, raw_count FROM read_parquet('{(ready_root / metric.score_table).as_posix()}')"
        ).fetchall()
        for h3_id, score, count in rows:
            i = index.get(h3_id)
            if i is None:
                # Zero-overall context categories may cover land cells where
                # no public-composite metric exists. They are audited inputs,
                # but must not expand the population ranked as `overall`.
                continue
            published[i, j] = float(score)
            raw_values[i, j] = float(count or 0)

    # Alternative normalizations from each table's canonical raw value,
    # direction-aware. For rodent v3.9 this field is an EB-shrunk rate despite
    # the backward-compatible parquet column name `raw_count`.
    minmax = np.full_like(raw_values, np.nan)
    zscore = np.full_like(raw_values, np.nan)
    for j, metric in enumerate(metrics):
        col = raw_values[:, j]
        mask = ~np.isnan(col)
        values = col[mask]
        lo, hi = float(np.min(values)), float(np.max(values))
        scaled = (values - lo) / (hi - lo) * 100 if hi > lo else np.full_like(values, 50.0)
        sd = float(np.std(values))
        z = 50 + 10 * (values - float(np.mean(values))) / sd if sd > 0 else np.full_like(values, 50.0)
        z = np.clip(z, 0, 100)
        if metric.direction is Direction.LOWER_IS_BETTER:
            scaled = 100 - scaled
            z = 100 - z
        minmax[mask, j] = scaled
        zscore[mask, j] = z

    weights = np.array(
        [
            CATEGORIES_BY_ID[m.category].weight_in_overall * m.weight_in_category
            for m in metrics
        ]
    )
    return frame, [m.id for m in metrics], weights, {
        "percentile": published,
        "minmax": minmax,
        "zscore": zscore,
    }


def composite(scores: np.ndarray, weights: np.ndarray, impute_means: np.ndarray | None) -> np.ndarray:
    """Weighted composite per cell.

    With ``impute_means`` None, weights renormalize over present sub-scores
    (production behaviour). Otherwise absent sub-scores are filled with the
    supplied citywide means and full weights apply.
    """
    if impute_means is not None:
        filled = np.where(np.isnan(scores), impute_means[None, :], scores)
        return filled @ weights / weights.sum()
    present = ~np.isnan(scores)
    weight_sum = present @ weights
    weighted = np.nansum(scores * weights[None, :], axis=1)
    out = np.full(scores.shape[0], np.nan)
    ok = weight_sum > 0
    out[ok] = weighted[ok] / weight_sum[ok]
    return out


def run(
    ready_root: Path,
    draws: int,
    seed: int,
    cell_output_dir: Path | None = None,
) -> tuple[dict, "np.ndarray", list[str]]:
    rng = np.random.default_rng(seed)
    frame, names, weights, score_sets = load_matrices(ready_root)
    n_cells = len(frame)
    toggle_idx = {m: names.index(m) for m in TOGGLE_METRICS if m in names}
    citywide_means = {
        norm: np.nanmean(scores, axis=0) for norm, scores in score_sets.items()
    }

    nominal = composite(score_sets["percentile"], weights, None)
    nominal_rank = rank_descending(nominal)

    results = np.empty((draws, n_cells), dtype=np.float32)
    draw_norm = rng.integers(0, len(NORMALIZATIONS), size=draws)
    draw_toggle = {m: rng.integers(0, 2, size=draws).astype(bool) for m in toggle_idx}
    draw_impute = rng.integers(0, 2, size=draws).astype(bool)

    for d in range(draws):
        norm = NORMALIZATIONS[draw_norm[d]]
        scores = score_sets[norm]
        w = weights * rng.uniform(1 - WEIGHT_NOISE, 1 + WEIGHT_NOISE, size=len(weights))
        for metric, included in draw_toggle.items():
            if not included[d]:
                w = w.copy()
                w[toggle_idx[metric]] = 0.0
        means = citywide_means[norm] if draw_impute[d] else None
        results[d] = composite(scores, w, means)

    ranks = np.empty_like(results)
    for d in range(draws):
        ranks[d] = rank_descending(results[d])

    # A cell whose only observed metrics are toggled off in a renormalizing
    # draw has no composite for that draw. Means over such empty slices are
    # legitimately NaN; numpy's warning about them is noise here.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        median = np.nanmedian(results, axis=0)
        lo = np.nanpercentile(results, INTERVAL[0], axis=0)
        hi = np.nanpercentile(results, INTERVAL[1], axis=0)
        rank_median = np.nanmedian(ranks, axis=0)
        rank_lo = np.nanpercentile(ranks, 5, axis=0)
        rank_hi = np.nanpercentile(ranks, 95, axis=0)

        mean_abs_rank_shift = float(np.nanmean(np.abs(rank_median - nominal_rank)))

        # The full interval spans defensible *methods*; this conditional one
        # holds normalization at the production choice and answers the
        # narrower question "given our method, how much do weights, the
        # configured overlap toggle and the missing-data rule move a score?".
        pct_draws = draw_norm == NORMALIZATIONS.index("percentile")
        pct_lo = np.nanpercentile(results[pct_draws], INTERVAL[0], axis=0)
        pct_hi = np.nanpercentile(results[pct_draws], INTERVAL[1], axis=0)

        toggle_effects = {}
        for metric, included in draw_toggle.items():
            with_it = np.nanmean(results[included], axis=0)
            without = np.nanmean(results[~included], axis=0)
            delta = np.abs(with_it - without)
            toggle_effects[metric] = {
                "mean_abs_score_delta": round(float(np.nanmean(delta)), 2),
                "p95_abs_score_delta": round(float(np.nanpercentile(delta, 95)), 2),
            }
        impute_delta = np.abs(
            np.nanmean(results[draw_impute], axis=0) - np.nanmean(results[~draw_impute], axis=0)
        )
    norm_spread = {}
    for k, norm in enumerate(NORMALIZATIONS):
        sel = draw_norm == k
        norm_spread[norm] = round(float(np.nanmean(np.nanmean(results[sel], axis=0))), 2)

    summary = {
        "generated": date.today().isoformat(),
        "seed": seed,
        "draws": draws,
        "cells": n_cells,
        "metrics": names,
        "design": {
            "weight_noise": WEIGHT_NOISE,
            "normalizations": list(NORMALIZATIONS),
            "toggles": list(toggle_idx),
            "missing_rules": ["renormalize", "impute_citywide_mean"],
            "interval_percentiles": list(INTERVAL),
        },
        "headline": {
            "median_interval_width": round(float(np.nanmedian(hi - lo)), 2),
            "p95_interval_width": round(float(np.nanpercentile(hi - lo, 95)), 2),
            "median_interval_width_production_norm": round(
                float(np.nanmedian(pct_hi - pct_lo)), 2
            ),
            "mean_abs_rank_shift": round(mean_abs_rank_shift, 1),
            "rank_shift_share_of_city": round(mean_abs_rank_shift / n_cells, 4),
            "toggle_effects": toggle_effects,
            "imputation_mean_abs_score_delta": round(float(np.nanmean(impute_delta)), 2),
            "normalization_citywide_means": norm_spread,
        },
    }

    per_cell = np.column_stack(
        [nominal, median, lo, hi, pct_lo, pct_hi, nominal_rank, rank_median, rank_lo, rank_hi]
    )
    artifact_path = write_per_cell(
        frame,
        per_cell,
        cell_output_dir or ready_root / "analysis",
    )
    write_publication_manifest(artifact_path, ready_root, summary)
    return summary, per_cell, frame


def rank_descending(values: np.ndarray) -> np.ndarray:
    """1 = best. NaN composites rank last and stay NaN in the output."""
    out = np.full(values.shape, np.nan)
    mask = ~np.isnan(values)
    order = np.argsort(-values[mask], kind="stable")
    ranks = np.empty(order.shape, dtype=float)
    ranks[order] = np.arange(1, order.size + 1)
    out[mask] = ranks
    return out


def write_per_cell(frame: list[str], per_cell: np.ndarray, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sensitivity_cells.parquet"
    temp_path = out_dir / f".{path.name}.{os.getpid()}.tmp"
    con = duckdb.connect()
    # lo95/hi95 span every perturbed assumption including the normalization
    # method itself; lo95_prodnorm/hi95_prodnorm hold normalization at the
    # production choice and answer the narrower "given our method" question.
    # The API serves both, labelled.
    con.execute(
        """
        CREATE TABLE t (
            h3_r9 VARCHAR, nominal DOUBLE, median DOUBLE, lo95 DOUBLE, hi95 DOUBLE,
            lo95_prodnorm DOUBLE, hi95_prodnorm DOUBLE,
            rank_nominal DOUBLE, rank_median DOUBLE, rank_p5 DOUBLE, rank_p95 DOUBLE
        )
        """
    )
    con.executemany(
        "INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (frame[i], *[None if np.isnan(v) else float(v) for v in per_cell[i]])
            for i in range(len(frame))
        ],
    )
    try:
        con.execute(
            f"COPY t TO '{temp_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_publication_manifest(
    artifact_path: Path,
    ready_root: Path,
    summary: dict,
) -> Path:
    """Publish the exact artifact/input snapshot accepted by the API."""
    inputs = {}
    for metric in h3_metrics():
        source = ready_root / metric.score_table
        if ready_publication_valid(
            ready_root,
            metric.score_table,
            metric.publication_manifest,
        ):
            inputs[metric.id] = {
                "path": metric.score_table,
                "sha256": _sha256(source),
                "size_bytes": source.stat().st_size,
            }
    manifest = {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "generated": summary["generated"],
        "seed": summary["seed"],
        "draws": summary["draws"],
        "artifact": {
            "filename": artifact_path.name,
            "sha256": _sha256(artifact_path),
            "size_bytes": artifact_path.stat().st_size,
            "row_count": summary["cells"],
            "columns": list(ARTIFACT_COLUMNS),
        },
        "input_score_tables": inputs,
    }
    path = artifact_path.with_name("sensitivity_cells.manifest.json")
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def render_markdown(summary: dict) -> str:
    h = summary["headline"]
    toggles = h["toggle_effects"]
    lines = [
        "# Sensitivity analysis",
        "",
        f"Generated {summary['generated']} by `backend/scripts/run_sensitivity_analysis.py` "
        f"(EXPANSION_PLAN item 1.4). {summary['draws']:,} Monte Carlo draws over "
        f"{summary['cells']:,} H3 r9 cells, seed {summary['seed']}.",
        "",
        "Each draw simultaneously perturbs sub-metric weights (x U(0.75, 1.25)), "
        "switches the normalization (published percentile / min-max / z-score, "
        "rebuilt from raw values), toggles the configured overlap metric, "
        "and switches the missing-data rule (renormalize vs "
        "impute citywide mean). Design constants follow the collected sources: "
        "COINr noisy weights at 0.25, CDC PLACES' 1,000-draw 95% interval, the "
        "OECD/JRC handbook's method-substitution and exclusion tests.",
        "",
        "## Headline numbers",
        "",
        f"- Median 95% interval width on the 0-100 score: **{h['median_interval_width']}** "
        f"points (95th percentile of widths: {h['p95_interval_width']}). Holding "
        "normalization at the production choice narrows the median width to "
        f"**{h['median_interval_width_production_norm']}** -- the difference is "
        "the price of the normalization method itself, the rest is weights, "
        "the flagged metrics and the missing-data rule.",
        f"- Mean absolute rank shift (median-of-draws vs nominal): "
        f"**{h['mean_abs_rank_shift']:.0f}** places out of {summary['cells']:,} "
        f"({100 * h['rank_shift_share_of_city']:.1f}% of the ranking).",
        "- `collision_transport` was removed in v3.8 and is not toggled in "
        "these draws.",
        f"- Dropping `311_sanitation` moves it by "
        f"**{toggles.get('311_sanitation', {}).get('mean_abs_score_delta', 'n/a')}** "
        f"(95th percentile {toggles.get('311_sanitation', {}).get('p95_abs_score_delta', 'n/a')}).",
        f"- Imputation vs renormalization: mean absolute difference "
        f"**{h['imputation_mean_abs_score_delta']}** points.",
        "- Citywide mean composite under each normalization: "
        + ", ".join(f"{k} {v}" for k, v in h["normalization_citywide_means"].items())
        + " -- the level differences are why scores must state their method version.",
        "",
        "## What this licenses",
        "",
        "Per-cell intervals and rank ranges are in "
        "`data/ready/analysis/sensitivity_cells.parquet` (untracked, "
        "regenerable with the seed). A published score can now carry its "
        "interval, and a rank claim ('safer than X% of the city') its range -- "
        "the acceptance criterion for item 1.4. The live toggle effect above "
        "quantifies the retained sanitation/rodent construct decision. The "
        "cross-category sanitation/housing overlap is excluded because "
        "building has zero overall weight; making that weight non-zero first "
        "requires exposure adjustment or a shared-construct cap and a fresh "
        "sensitivity run.",
        "",
        "Publication is atomic: `sensitivity_cells.parquet` is paired with "
        "`sensitivity_cells.manifest.json`, which records methodology version, "
        "draw count, seed, row/schema checks, artifact SHA-256 and every input "
        "score-table SHA-256. The API fails closed when either file or any "
        "input snapshot changes. Its public headline maps the production-"
        "normalization 95% interval across fixed 20-point tiers; the point "
        "estimate remains secondary detail.",
        "",
        "## Stated limits",
        "",
        "- ZIP-grain metrics (ems, fire, parks) are held out, matching how "
        "production renormalizes when a ZIP lookup fails; their weight share "
        "is redistributed identically in every draw.",
        "- `restaurant_context`'s inspection-quality adjustment exists only in "
        "the percentile branch; min-max and z rebuild from counts alone.",
        "- Absence from a sparse risk table (`aep`: 586 cells citywide) is "
        "treated as missing, as production does. Whether absence should score "
        "as good news for count-of-bad-thing metrics is an open product "
        "question this analysis surfaces but does not settle.",
        "- The `building` category's weight is 0.0 nominally and stays 0 under "
        "multiplicative noise -- the degenerate case the weight-sensitivity "
        "literature warns about. Deciding building's status (PROJECT_PLAN "
        "P0-02) is prerequisite to including it here meaningfully.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--ready-root", type=Path, default=REPO_ROOT / "data" / "ready")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs" / "methodology")
    args = parser.parse_args()

    summary, _, _ = run(args.ready_root, args.draws, args.seed)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "sensitivity-analysis.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.out_dir / "sensitivity-analysis.md").write_text(render_markdown(summary))
    print(json.dumps(summary["headline"], indent=2))


if __name__ == "__main__":
    main()
