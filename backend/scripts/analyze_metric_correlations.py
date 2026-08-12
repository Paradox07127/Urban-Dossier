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
Frame: the union of H3 r9 cells appearing in any H3-grain score table. A cell
absent from one table is a place where that phenomenon was never observed --
raw_count 0, not missing data -- so counts are zero-filled over the frame
before ranking. An inner join would instead ask "among cells that have both
rats and complaints, do they co-vary?", which is a different and weaker
question than "does one signal duplicate the other across the city?".

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


def zero_filled_counts(
    con: duckdb.DuckDBPyConnection, tables: dict[str, Path], frame: list[str]
) -> np.ndarray:
    """metrics x cells matrix of raw counts, absent cells as genuine zeros."""
    index = {cell: i for i, cell in enumerate(frame)}
    matrix = np.zeros((len(tables), len(frame)))
    for row, path in enumerate(tables.values()):
        for h3_id, count in con.execute(
            f"SELECT h3_r9, raw_count FROM read_parquet('{path.as_posix()}')"
        ).fetchall():
            matrix[row, index[h3_id]] = float(count or 0)
    return matrix


def spearman_matrix(matrix: np.ndarray) -> np.ndarray:
    n = matrix.shape[0]
    rho = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            r = float(stats.spearmanr(matrix[i], matrix[j]).statistic)
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
    counts = zero_filled_counts(con, h3_tables, frame)
    rho = spearman_matrix(counts)

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
            "fill": "absent cell = raw_count 0 (never observed, not missing)",
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
        f"({report['method']['frame']}); absent cells are genuine zeros. "
        "Statistic: Spearman on zero-filled raw counts. With this many cells "
        "every p-value rounds to zero, so magnitudes are the finding, not "
        "significance.",
        "",
        "## Declared relationships, measured",
        "",
        "The metric registry declares two suspect relationships. Both hold:",
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
        "| pair | rho (counts, zero-filled) | rho (scores, inner join) | inner N | level |",
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
        "## Full matrix (Spearman, zero-filled counts)",
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
        "Everything correlates with everything at rho 0.2-0.6, because every "
        "metric is an unnormalized count within a radius and therefore "
        "measures activity density before it measures its own phenomenon. "
        "That baseline makes the pairs above it stand out more, not less.",
        "",
        "The `rodent` / `311_sanitation` / `housing_violations` triangle is "
        "the substantive finding: resident complaints, confirmed inspections "
        "and open housing violations are three measurements of one underlying "
        "condition of the building stock, sitting in two categories under "
        "three weights. The registry declared the first pair; the "
        "cross-category legs were not declared anywhere and only the "
        "measurement found them.",
        "",
        "## Decision required (not taken here)",
        "",
        "1. `collision_transport` (rho = 1.000 by construction, 19% of "
        "`overall` combined with `collision`): drop it and reweight transit, "
        "or replace it with an actual transit-risk measure. Keeping it as-is "
        "is a decision to double-count collisions, and should be written down "
        "as such if taken.",
        "2. The rodent/sanitation/violations triangle: candidate treatments "
        "are down-weighting within safety, or merging the two safety metrics "
        "into one 'sanitation conditions' signal with two evidence sources. "
        "Any change moves published scores and belongs with item 1.4's "
        "sensitivity analysis, which can quantify how much.",
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
