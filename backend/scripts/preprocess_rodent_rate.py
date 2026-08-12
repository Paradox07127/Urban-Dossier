"""Build the inspection-anchored rodent score: DOHMH's own construction.

Replaces the count-of-positives metric with the rate DOHMH itself uses to
designate Rat Mitigation Zones: the share of *initial* inspections that found
active rat signs. The complaint-count predecessor measured inspection volume
as much as rats -- and volume follows complaints, which follow reporting
propensity (Walsh 2014; Kontokosta & Hong 2021, both in the methodology
reference set). Conditioning on being inspected removes the volume term; what
remains of reporting bias is selection into inspection, which is disclosed
below rather than hidden.

    rate(cell) = failed-for-rat-activity / initial inspections, 3-year window,
                 shrunk toward the citywide rate by an empirical-Bayes beta
                 prior fitted by method of moments

Decisions, written down because each moves numbers:

* Initial inspections only. Compliance visits condition on a prior failure
  and treatments condition on known activity; both would inflate the rate
  exactly where the city is already responding.
* "Failed for Rat Activity" and "Failed for Rat Activity and Other Reason"
  both count as active signs; "Failed for Other Reason" does not.
* Three-year window. Rat conditions turn over faster than the five-year crash
  window; DOHMH indexing runs in roughly annual cycles, and three of them
  give a typical scored cell tens of inspections.
* Date hygiene: the raw file carries typo dates from 1918 to 2045. Rows
  outside [window_lo, newest sane date] are dropped and counted.
* Shrinkage: a cell with 2 inspections and 1 failure is not a 50%-rat block.
  Rates are shrunk toward the citywide mean with a beta prior fitted to the
  observed cell-level distribution by method of moments -- the same
  empirical-Bayes treatment the reporting-bias literature applies to
  complaint-to-confirmation ratios. Prior strength is data-derived and
  recorded in the manifest, not hand-picked.
* Known limitation, disclosed: initial inspections include complaint-driven
  ones, and the public data cannot separate proactive indexing from them. The
  rate therefore still oversamples complained-about places; what it no longer
  does is scale with complaint volume.

Output: safety/rodent_rate_scores_h3.parquet
    (h3_r9, raw_count = shrunk rate, inspections, rat_positive, raw_rate,
     score = percentile of shrunk rate, lower better)
plus rodent_rate.manifest.json.

Usage:
    python backend/scripts/preprocess_rodent_rate.py [--raw-root PATH]
        [--ready-root PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preprocess_common import percentile_score  # noqa: E402

DEFAULT_RAW = Path("/mnt/data/urban-dossier-state/datasets/raw")
REPO_ROOT = Path(__file__).resolve().parents[2]

WINDOW_YEARS = 3
RAT_RESULTS = ("Failed for Rat Activity", "Failed for Rat Activity and Other Reason")
BBOX = (40.4, 41.0, -74.3, -73.6)


def fit_beta_prior(rates: np.ndarray, weights: np.ndarray) -> tuple[float, float]:
    """Method-of-moments beta prior over cell rates, inspection-weighted.

    Returns (alpha, beta). Falls back to a weak uniform-ish prior if the
    observed variance is degenerate.
    """
    mean = float(np.average(rates, weights=weights))
    var = float(np.average((rates - mean) ** 2, weights=weights))
    if var <= 0 or mean <= 0 or mean >= 1:
        return 1.0, max(1.0 / max(mean, 1e-6) - 1.0, 1.0)
    common = mean * (1 - mean) / var - 1
    common = max(common, 1.0)
    return mean * common, (1 - mean) * common


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--ready-root", type=Path, default=REPO_ROOT / "data" / "ready")
    args = parser.parse_args()

    csv = args.raw_root / "environment" / "rodent_inspections.csv"
    con = duckdb.connect()

    hi = con.execute(
        f"""SELECT max(d) FROM (
              SELECT try_cast(INSPECTION_DATE AS DATE) AS d
              FROM read_csv_auto('{csv.as_posix()}', sample_size=20000))
            WHERE d <= current_date"""
    ).fetchone()[0]
    lo = hi.replace(year=hi.year - WINDOW_YEARS)

    rat_list = ", ".join(f"'{r}'" for r in RAT_RESULTS)
    rows = con.execute(
        f"""
        SELECT LATITUDE, LONGITUDE,
               (RESULT IN ({rat_list}))::INT AS rat
        FROM read_csv_auto('{csv.as_posix()}', sample_size=20000)
        WHERE INSPECTION_TYPE = 'Initial'
          AND try_cast(INSPECTION_DATE AS DATE) BETWEEN DATE '{lo}' AND DATE '{hi}'
          AND LATITUDE BETWEEN {BBOX[0]} AND {BBOX[1]}
          AND LONGITUDE BETWEEN {BBOX[2]} AND {BBOX[3]}
        """
    ).fetchall()

    import h3

    inspections: dict[str, int] = {}
    positives: dict[str, int] = {}
    for lat, lon, rat in rows:
        cell = h3.latlng_to_cell(float(lat), float(lon), 9)
        inspections[cell] = inspections.get(cell, 0) + 1
        positives[cell] = positives.get(cell, 0) + int(rat)

    cells = sorted(inspections)
    n = np.array([inspections[c] for c in cells], dtype=float)
    k = np.array([positives[c] for c in cells], dtype=float)
    raw_rate = k / n

    alpha, beta = fit_beta_prior(raw_rate, n)
    shrunk = (k + alpha) / (n + alpha + beta)

    import pandas as pd

    scored = pd.DataFrame(
        {
            "h3_r9": cells,
            # Shrunk rate under the conventional raw-value column name so the
            # correlation and sensitivity tooling reads this table unmodified.
            "raw_count": shrunk,
            "inspections": n.astype(int),
            "rat_positive": k.astype(int),
            "raw_rate": np.round(raw_rate, 4),
        }
    )
    scored["score"] = percentile_score(scored["raw_count"], access_mode=False)

    out_dir = args.ready_root / "safety"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "rodent_rate_scores_h3.parquet"
    con.execute("CREATE OR REPLACE TABLE t AS SELECT * FROM scored")
    con.execute(f"COPY t TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    manifest = {
        "generated": date.today().isoformat(),
        "window": [str(lo), str(hi)],
        "initial_inspections": int(n.sum()),
        "rat_positive": int(k.sum()),
        "citywide_rate": round(float(k.sum() / n.sum()), 4),
        "cells_scored": len(cells),
        "median_inspections_per_cell": float(np.median(n)),
        "beta_prior": {"alpha": round(alpha, 3), "beta": round(beta, 3),
                        "strength": round(alpha + beta, 1)},
        "selection_note": (
            "Initial inspections include complaint-driven ones; proactive "
            "indexing is not separable in the public data. The rate conditions "
            "away complaint volume but not selection into inspection."
        ),
    }
    (out_dir / "rodent_rate.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
