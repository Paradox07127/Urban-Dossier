"""Cross-sectional correlation between sub-metrics -- EXPANSION_PLAN item 1.3.

The scoring system aggregates 18 sub-metrics with expert weights, and the
OECD/JRC handbook's multivariate-analysis step exists to catch what that can
hide: two metrics that look like independent corroborating signals but move
together, so their weights silently stack. The registry already *declares* two
suspect relationships (`collision_transport` is a byte copy of `collision`;
`311_sanitation`'s filter admits rodent complaints beside `rodent`). This
script measures them -- and everything else -- instead of trusting the
declarations.

Method
------
Frame: the union of H3 r9 cells appearing in any H3-grain score table.
Absence is metric-specific and comes from the registry. For event/inventory
counts it means an observed zero and is filled with 0. For rates such as the
inspection-anchored rodent metric it means no denominator was observed and
stays missing. Spearman is then computed pairwise over cells where both raw
values are defined.

Statistic: Spearman rank correlation, because every raw series here is a
zero-inflated, heavily right-skewed count and Pearson on such data mostly
measures the outliers. Two views are computed:

* zero-filled raw counts over the full frame (primary -- the double-counting
  question);
* published 0-100 scores on the pairwise inner join (secondary -- what a
  consumer of two scores actually experiences where both exist).

ZIP-grain metrics (ems_response, fire_response, parks_access) are correlated
among themselves on their common ZIPs. They are not mixed into the H3 matrix:
correlating across grains requires an allocation rule, and inventing one here
would manufacture exactly the kind of unstated modelling step this analysis
exists to expose.

With ~7,000 cells, p-values are all indistinguishable from zero and say
nothing useful; magnitudes are the finding. Thresholds follow JRC COINr
practice: |rho| >= 0.9 flagged collinear, >= 0.7 flagged high.

Output: a JSON report (machine-readable, consumed by tests) and a Markdown
report (for the methodology page, item 1.6) under docs/methodology/. The
report *states* findings; weight changes are a product decision it feeds.

Usage:
    python backend/scripts/analyze_metric_correlations.py [--ready-root PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
from scipy import stats

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from urban_dossier_backend.metrics import (  # noqa: E402
    METRICS,
    duplicated_sources,
    overlapping_pairs,
)

COLLINEAR = 0.9
HIGH = 0.7

# Raw-value column per ZIP table; H3 tables all carry raw_count.
ZIP_VALUE_COLUMNS = {
    "ems_response": "avg_response_seconds",
    "fire_response": "avg_response_seconds",
    "parks_access": "total_value",
}


def h3_metric_tables(ready_root: Path) -> dict[str, Path]:
    """Metric id -> score-table path, for H3-grain metrics with a table on disk."""
    out: dict[str, Path] = {}
    for metric in METRICS:
        if metric.spatial_grain.value != "h3_r9":
            continue
        path = ready_root / metric.score_table
        if path.exists():
            out[metric.id] = path
    return out


def zip_metric_tables(ready_root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for metric in METRICS:
        if metric.spatial_grain.value != "zip":
            continue
        path = ready_root / metric.score_table
        if path.exists():
            out[metric.id] = path
    return out


def build_frame(con: duckdb.DuckDBPyConnection, tables: dict[str, Path]) -> list[str]:
    """Union of cells across every table, sorted for determinism."""
    union = " UNION ".join(
        f"SELECT h3_r9 FROM read_parquet('{p.as_posix()}')" for p in tables.values()
    )
    return sorted(r[0] for r in con.execute(union).fetchall())


def raw_value_matrix(
    con: duckdb.DuckDBPyConnection,
    tables: dict[str, Path],
    frame: list[str],
    absence_means_zero: dict[str, bool] | None = None,
) -> np.ndarray:
    """Metrics x cells raw-value matrix with registry-defined absence semantics."""
    index = {cell: i for i, cell in enumerate(frame)}
    policy = absence_means_zero or {name: True for name in tables}
    matrix = np.full((len(tables), len(frame)), np.nan)
    for row, (name, path) in enumerate(tables.items()):
        if policy.get(name, True):
            matrix[row, :] = 0.0
        for h3_id, value in con.execute(
            f"SELECT h3_r9, raw_count FROM read_parquet('{path.as_posix()}')"
        ).fetchall():
            matrix[row, index[h3_id]] = np.nan if value is None else float(value)
    return matrix


def zero_filled_counts(
    con: duckdb.DuckDBPyConnection, tables: dict[str, Path], frame: list[str]
) -> np.ndarray:
    """Compatibility helper for fixtures whose metrics are all event counts."""
    return raw_value_matrix(con, tables, frame)


def spearman_matrix(matrix: np.ndarray) -> np.ndarray:
    n = matrix.shape[0]
    rho = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            present = np.isfinite(matrix[i]) & np.isfinite(matrix[j])
            if int(present.sum()) < 3:
                r = float("nan")
            else:
                a, b = matrix[i, present], matrix[j, present]
                r = (
                    float("nan")
                    if len(set(a)) < 2 or len(set(b)) < 2
                    else float(stats.spearmanr(a, b).statistic)
                )
            rho[i, j] = rho[j, i] = r
    return rho


def score_correlation_inner(
    con: duckdb.DuckDBPyConnection, path_a: Path, path_b: Path
) -> tuple[float, int]:
    """Spearman of published scores where both tables have the cell."""
    rows = con.execute(
        f"""
        SELECT a.score, b.score
        FROM read_parquet('{path_a.as_posix()}') a
        JOIN read_parquet('{path_b.as_posix()}') b USING (h3_r9)
        """
    ).fetchall()
    if len(rows) < 3:
        return float("nan"), len(rows)
    a, b = zip(*rows)
    # A constant series has no ranks to correlate; scipy warns and returns
    # nan. Return the nan without the warning -- it is an expected outcome for
    # sparse tables whose overlap happens to be uniform, not a problem.
    if len(set(a)) < 2 or len(set(b)) < 2:
        return float("nan"), len(rows)
    return float(stats.spearmanr(a, b).statistic), len(rows)


def flag_pairs(names: list[str], rho: np.ndarray) -> list[dict]:
    """Every pair at or above HIGH, strongest first."""
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            r = rho[i, j]
            if abs(r) >= HIGH:
                out.append(
                    {
                        "pair": [names[i], names[j]],
                        "rho": round(r, 3),
                        "level": "collinear" if abs(r) >= COLLINEAR else "high",
                    }
                )
    return sorted(out, key=lambda e: -abs(e["rho"]))


def analyze(ready_root: Path) -> dict:
    con = duckdb.connect()
    h3_tables = h3_metric_tables(ready_root)
    frame = build_frame(con, h3_tables)
    names = list(h3_tables)
    values = raw_value_matrix(
        con,
        h3_tables,
        frame,
        {metric.id: metric.absence_means_zero for metric in METRICS},
    )
    rho = spearman_matrix(values)

    flagged = flag_pairs(names, rho)
    # The declared relationships, checked whether flagged or not, so the report
    # always answers "did the declarations hold?" explicitly.
    declared = []
    for src, ids in duplicated_sources().items():
        pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
        for a, b in pairs:
            if a in names and b in names:
                declared.append(
                    {
                        "pair": [a, b],
                        "kind": "duplicated_source",
                        "source": src,
                        "rho": round(rho[names.index(a), names.index(b)], 3),
                    }
                )
    for a, b in overlapping_pairs():
        if a in names and b in names:
            declared.append(
                {
                    "pair": [a, b],
                    "kind": "declared_overlap",
                    "rho": round(rho[names.index(a), names.index(b)], 3),
                }
            )

    # Secondary view: published scores, inner join, for each flagged pair.
    for entry in flagged:
        a, b = entry["pair"]
        score_rho, n = score_correlation_inner(con, h3_tables[a], h3_tables[b])
        entry["score_rho_inner"] = round(score_rho, 3)
        entry["inner_n"] = n

    # ZIP grain, among themselves.
    zip_tables = zip_metric_tables(ready_root)
    zip_values: dict[str, dict[str, float]] = {}
    for metric_id, path in zip_tables.items():
        column = ZIP_VALUE_COLUMNS[metric_id]
        zip_values[metric_id] = {
            z: float(v)
            for z, v in con.execute(
                f"SELECT zip, {column} FROM read_parquet('{path.as_posix()}')"
            ).fetchall()
            if v is not None
        }
    zip_names = list(zip_values)
    common = sorted(set.intersection(*(set(v) for v in zip_values.values()))) if zip_values else []
    zip_pairs = []
    for i, a in enumerate(zip_names):
        for b in zip_names[i + 1:]:
            va = [zip_values[a][z] for z in common]
            vb = [zip_values[b][z] for z in common]
            zip_pairs.append(
                {
                    "pair": [a, b],
                    "rho": round(float(stats.spearmanr(va, vb).statistic), 3),
                    "n_zips": len(common),
                }
            )

    return {
        "generated": date.today().isoformat(),
        "method": {
            "frame": "union of H3 r9 cells across all H3 score tables",
            "frame_cells": len(frame),
            "absence_policy": {
                metric.id: (
                    "zero" if metric.absence_means_zero else "missing"
                )
                for metric in METRICS
                if metric.id in h3_tables
            },
            "statistic": "spearman",
            "thresholds": {"collinear": COLLINEAR, "high": HIGH},
        },
        "h3_metrics": names,
        "matrix": [[round(float(v), 3) for v in row] for row in rho],
        "flagged_pairs": flagged,
        "declared_relationships": declared,
        "zip_pairs": zip_pairs,
    }


def render_markdown(report: dict) -> str:
    names = report["h3_metrics"]
    rho = report["matrix"]
    lines = [
        "# Sub-metric correlation report",
        "",
        f"Generated {report['generated']} by `backend/scripts/analyze_metric_correlations.py` "
        "(EXPANSION_PLAN item 1.3).",
        "",
        f"Frame: {report['method']['frame_cells']:,} H3 r9 cells "
        f"({report['method']['frame']}). Count metrics treat absence as zero; "
        "rate metrics keep absence missing and use pairwise-complete cells. "
        "Statistic: Spearman on raw values. With this many cells every p-value "
        "rounds to zero, so magnitudes are the finding, not significance.",
        "",
        "## Declared relationships, measured",
        "",
        "The metric registry's declared relationships are measured below:",
        "",
    ]
    for entry in report["declared_relationships"]:
        a, b = entry["pair"]
        what = (
            f"same source file (`{entry['source']}`)"
            if entry["kind"] == "duplicated_source"
            else "declared overlap"
        )
        lines.append(f"- `{a}` vs `{b}` -- {what}: **rho = {entry['rho']:+.3f}**")
    lines += [
        "",
        "## All pairs at |rho| >= 0.7",
        "",
        "| pair | rho (raw, metric-aware absence) | rho (scores, inner join) | inner N | level |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in report["flagged_pairs"]:
        a, b = entry["pair"]
        lines.append(
            f"| `{a}` / `{b}` | {entry['rho']:+.3f} | {entry['score_rho_inner']:+.3f} "
            f"| {entry['inner_n']:,} | {entry['level']} |"
        )
    lines += [
        "",
        "## ZIP-grain metrics",
        "",
    ]
    for entry in report["zip_pairs"]:
        a, b = entry["pair"]
        lines.append(f"- `{a}` vs `{b}`: rho = {entry['rho']:+.3f} (N = {entry['n_zips']} ZIPs)")
    lines += [
        "",
        "## Full matrix (Spearman, raw values with metric-aware absence)",
        "",
        "| | " + " | ".join(f"`{n}`" for n in names) + " |",
        "| --- |" + " --- |" * len(names),
    ]
    for i, name in enumerate(names):
        lines.append(
            f"| `{name}` | " + " | ".join(f"{rho[i][j]:+.2f}" for j in range(len(names))) + " |"
        )
    lines += [
        "",
        "## Reading the numbers",
        "",
        "Most count metrics share a positive activity-density baseline: busy, "
        "densely observed cells contain more of many phenomena. Rate metrics "
        "do not get synthetic zeros outside their observed denominators, so "
        "their rows answer a pairwise conditional question instead.",
        "",
        "The inspection-anchored `rodent` rate is no longer highly correlated "
        "with `311_sanitation` or `housing_violations`; this is the intended "
        "v3.9 result. The remaining collinear pair is `311_sanitation` / "
        "`housing_violations`, both count surfaces that still share the "
        "activity-density baseline and underlying building conditions.",
        "",
        "## Decisions",
        "",
        "1. Resolved in v3.8: `collision_transport` was removed and transit "
        "was reweighted; the measured replacement remains an unregistered "
        "candidate.",
        "2. Resolved in v3.9: rodent changed from positive-inspection counts to "
        "an inspection failure rate; uninspected cells are missing, not zero.",
        "3. Resolved in v3.9: retain the current weights for the "
        "`311_sanitation` / `housing_violations` pair. The relationship is "
        "now declared in the registry, but it crosses safety and building, "
        "and building has zero overall weight, so housing contributes exactly "
        "zero to the current public composite. This is not permission to add "
        "building later: before any non-zero overall or priority weight, "
        "replace the shared activity-density count with an exposure-adjusted "
        "rate or cap the pair as one construct, then rerun correlation and "
        "sensitivity.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ready-root",
        type=Path,
        default=REPO_ROOT / "data" / "ready",
        help="Ready-layer root holding the score tables",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "docs" / "methodology",
        help="Where to write the JSON and Markdown reports",
    )
    args = parser.parse_args()

    report = analyze(args.ready_root)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "metric-correlations.json"
    md_path = args.out_dir / "metric-correlations.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    md_path.write_text(render_markdown(report))
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(f"flagged pairs: {len(report['flagged_pairs'])}")
    for entry in report["flagged_pairs"]:
        print(f"  {entry['pair'][0]} / {entry['pair'][1]}: {entry['rho']:+.3f} ({entry['level']})")


if __name__ == "__main__":
    main()
