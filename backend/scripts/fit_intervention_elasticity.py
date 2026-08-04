#!/usr/bin/env python3
"""Fit empirical count->score curves used by POST /api/simulate.

What this does
--------------
Scores are read from prebuilt H3/ZIP score tables, not recomputed from raw
counts at query time, so "add 3 parks -> sub-score changes by X" has no basis
anywhere in the online code. This script derives that mapping from the
published data instead of inventing a coefficient.

Each score table already carries the raw asset count next to the score, so for
every intervention we can measure what cells at each count level actually
score, citywide:

    curve[count] = mean(score) over all units with that raw count

The curve is made monotone non-decreasing (the underlying scoring is a rank
transform, so score must not fall as assets are added; local dips are sampling
noise) and a linear coefficient is fitted for extrapolation past the observed
range.

What this is NOT
----------------
This is correlational, not causal. It says "places with N+3 of these score S",
not "building 3 here will produce S". Neighbourhoods that already have more
assets differ in many other ways. Every response from /api/simulate carries
this caveat, and the artifact records n, the Spearman correlation and the
observed count range so a weak fit is visible rather than implied.

Usage:
    python backend/scripts/fit_intervention_elasticity.py \
        --ready-root data/ready --out data/cache/simulation/elasticity.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


# intervention -> where its evidence lives. category/sub_dataset must match
# categories.CATEGORY_CONFIG so the projected sub-score can be fed back through
# the real weighting functions.
INTERVENTION_SOURCES = {
    "bike_lane": {
        "category": "transit",
        "sub_dataset": "bike_routes",
        "score_table": "transit/bike_routes_scores_h3.parquet",
        "grain": "h3_r9",
        "count_column": "raw_count",
    },
    "bus_stop": {
        "category": "transit",
        "sub_dataset": "bus",
        "score_table": "transit/bus_scores_h3.parquet",
        "grain": "h3_r9",
        "count_column": "raw_count",
    },
    "toilet": {
        "category": "amenities",
        "sub_dataset": "public_toilets",
        "score_table": "amenities/toilets_scores_h3.parquet",
        "grain": "h3_r9",
        "count_column": "raw_count",
    },
    "linknyc": {
        "category": "amenities",
        "sub_dataset": "linknyc",
        "score_table": "amenities/linknyc_scores_h3.parquet",
        "grain": "h3_r9",
        "count_column": "raw_count",
    },
    "park": {
        "category": "amenities",
        "sub_dataset": "parks_access",
        "score_table": "amenities/parks_scores_zip.parquet",
        "grain": "zip",
        "count_column": "record_count",
    },
}

MAX_CURVE_POINTS = 60


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation without scipy; ties get average ranks."""

    if len(xs) < 3:
        return None

    def _ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        index = 0
        while index < len(order):
            end = index
            while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
                end += 1
            average = (index + end) / 2.0 + 1.0
            for position in range(index, end + 1):
                ranks[order[position]] = average
            index = end + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = sum((a - mx) ** 2 for a in rx)
    den_y = sum((b - my) ** 2 for b in ry)
    if den_x <= 0 or den_y <= 0:
        return None
    return round(num / (den_x * den_y) ** 0.5, 4)


def _linear_slope(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope, used only to extrapolate past the observed range."""

    if len(xs) < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 6)


def fit_one(con, ready_root: Path, spec: dict) -> dict:
    path = ready_root / spec["score_table"]
    if not path.exists():
        return {"available": False, "reason": f"score table not published at {path}"}

    count_col = spec["count_column"]
    rows = con.execute(
        f'SELECT "{count_col}" AS c, score AS s '
        f"FROM read_parquet('{path.as_posix()}') "
        f'WHERE "{count_col}" IS NOT NULL AND score IS NOT NULL'
    ).fetchall()
    if len(rows) < 10:
        return {"available": False, "reason": f"only {len(rows)} usable rows"}

    counts = [float(r[0]) for r in rows]
    scores = [float(r[1]) for r in rows]

    grouped: dict[float, list[float]] = {}
    for count, score in zip(counts, scores):
        grouped.setdefault(count, []).append(score)

    curve = [(count, statistics.fmean(values)) for count, values in sorted(grouped.items())]

    # The published scoring is a rank transform: adding assets must never lower
    # the score. Enforce monotonicity so noise in sparse bins cannot produce a
    # negative "improvement" when the agent adds capacity.
    monotone: list[tuple[float, float]] = []
    running = float("-inf")
    for count, mean_score in curve:
        running = max(running, mean_score)
        monotone.append((count, round(running, 3)))

    # Keep the artifact small: thin evenly, always keeping both endpoints.
    if len(monotone) > MAX_CURVE_POINTS:
        step = len(monotone) / MAX_CURVE_POINTS
        thinned = [monotone[int(i * step)] for i in range(MAX_CURVE_POINTS)]
        if thinned[-1] != monotone[-1]:
            thinned.append(monotone[-1])
        monotone = thinned

    return {
        "available": True,
        "category": spec["category"],
        "sub_dataset": spec["sub_dataset"],
        "score_table": spec["score_table"],
        "grain": spec["grain"],
        "count_column": count_col,
        "n_units": len(rows),
        "count_range": [min(counts), max(counts)],
        "score_range": [round(min(scores), 3), round(max(scores), 3)],
        "spearman_count_vs_score": _spearman(counts, scores),
        "linear_slope_per_unit": _linear_slope(counts, scores),
        "curve": [[c, s] for c, s in monotone],
        "note": (
            "Empirical conditional mean of the published score at each observed "
            "asset count. Correlational, not causal."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-root", default="data/ready")
    parser.add_argument("--out", default="data/cache/simulation/elasticity.json")
    args = parser.parse_args()

    import duckdb

    ready_root = Path(args.ready_root)
    con = duckdb.connect()
    try:
        fitted = {
            name: fit_one(con, ready_root, spec)
            for name, spec in INTERVENTION_SOURCES.items()
        }
    finally:
        con.close()

    artifact = {
        "method": "empirical_conditional_mean",
        "causal": False,
        "caveat": (
            "Derived from citywide cross-sectional data: places that already "
            "have more of an asset also differ in other ways. Treat as an "
            "association-based reference point, not a forecast."
        ),
        "ready_root": str(ready_root),
        "interventions": fitted,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    for name, entry in fitted.items():
        if entry.get("available"):
            print(
                f"{name:10s} n={entry['n_units']:<6} "
                f"spearman={entry['spearman_count_vs_score']} "
                f"counts={entry['count_range']} points={len(entry['curve'])}"
            )
        else:
            print(f"{name:10s} UNAVAILABLE: {entry['reason']}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
