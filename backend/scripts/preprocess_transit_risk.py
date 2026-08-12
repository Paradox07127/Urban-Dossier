"""Build the transit_risk score table: severity-weighted casualty density.

The successor to the removed `collision_transport`, built to the construction
NYC DOT itself uses for Vision Zero Priority Corridors (2023 Borough
Pedestrian Safety Action Plan update: pedestrian KSI per street-mile) and
FHWA-SA-18-032's recommended facility-scale exposure proxy when volume data is
unavailable -- both saved in the methodology reference set. Unlike its
predecessor, this is an independent measurement: a different crash subset
(pedestrian and cyclist casualties only), a different statistic (severity-
weighted), and a different denominator (street length, not nothing).

    rate(cell) = [ 3 x (ped + cyclist killed) + 1 x (ped + cyclist injured) ]
                 / street_miles(cell),  over the trailing 5 years

Decisions, written down because each one moves numbers:

* Severity weights 3:1. Public MVC data carries killed/injured only -- no
  KABCO A-C grades -- so killed-vs-injured is the honest maximum resolution.
  The 3:1 ratio is a declared choice, not a standard, and belongs in the
  sensitivity analysis alongside the other weights.
* The 5-year window anchors to the newest crash in the data, not to today.
  Upstream updates are paused (fix expected Aug 2026); anchoring at max-date
  keeps the window five actual years of data instead of quietly shrinking.
* Street miles count RW_TYPE 1 (streets), 3 (bridges) and 10 (alleys), built
  STATUS only. Limited-access highways and ramps are excluded: pedestrians
  are not exposed there, and their miles would dilute the denominator exactly
  where the numerator cannot occur. Paths and boardwalks are excluded for the
  mirror-image reason -- no vehicles.
* Line length is apportioned to H3 r9 cells by sampling each segment every
  ~20 m, each sample carrying an equal share of the segment's length. Cheap,
  and errs by at most one sample spacing at cell borders.
* Cells are scored only if they contain street length; casualties falling in
  cells with none (park interiors, geocoding artefacts) are dropped and
  counted in the manifest. Miles are floored at 0.05 (~80 m) in the divisor
  so a stub of roadway with one injury cannot mint an absurd rate.
* The final score is the empirical percentile of the rate (lower rate =
  better), through the same `percentile_score` every other metric uses.

Output: transit/transit_risk_scores_h3.parquet with columns
    h3_r9, raw_count (= the casualty rate, kept under the conventional column
    name so the correlation and sensitivity tooling reads it unmodified),
    casualties, street_miles, score
plus transit_risk.manifest.json beside it recording the window and drops.

Usage:
    python backend/scripts/preprocess_transit_risk.py
        [--raw-root PATH] [--ready-root PATH]
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

SEVERITY_KILLED = 3.0
SEVERITY_INJURED = 1.0
WINDOW_YEARS = 5
STREET_RW_TYPES = (1, 3, 10)   # street, bridge, alley
BUILT_STATUS = 2
SAMPLE_SPACING_M = 20.0
MIN_MILES_DIVISOR = 0.05

# NYC bounding box, same guard the main preprocessing driver applies.
BBOX = (40.4, 41.0, -74.3, -73.6)


def casualty_cells(con: duckdb.DuckDBPyConnection, mvc_csv: Path) -> tuple[dict[str, float], str, str, int]:
    """Severity-weighted ped+cyc casualties per H3 r9 cell over the window."""
    import h3

    hi = con.execute(
        f"""SELECT max(try_cast("CRASH DATE" AS DATE))
            FROM read_csv_auto('{mvc_csv.as_posix()}', sample_size=20000)"""
    ).fetchone()[0]
    lo = hi.replace(year=hi.year - WINDOW_YEARS)

    rows = con.execute(
        f"""
        SELECT LATITUDE, LONGITUDE,
               coalesce("NUMBER OF PEDESTRIANS KILLED", 0) + coalesce("NUMBER OF CYCLIST KILLED", 0) AS killed,
               coalesce("NUMBER OF PEDESTRIANS INJURED", 0) + coalesce("NUMBER OF CYCLIST INJURED", 0) AS injured
        FROM read_csv_auto('{mvc_csv.as_posix()}', sample_size=20000)
        WHERE try_cast("CRASH DATE" AS DATE) BETWEEN DATE '{lo}' AND DATE '{hi}'
          AND LATITUDE BETWEEN {BBOX[0]} AND {BBOX[1]}
          AND LONGITUDE BETWEEN {BBOX[2]} AND {BBOX[3]}
          AND (coalesce("NUMBER OF PEDESTRIANS KILLED", 0) + coalesce("NUMBER OF CYCLIST KILLED", 0)
             + coalesce("NUMBER OF PEDESTRIANS INJURED", 0) + coalesce("NUMBER OF CYCLIST INJURED", 0)) > 0
        """
    ).fetchall()

    per_cell: dict[str, float] = {}
    for lat, lon, killed, injured in rows:
        cell = h3.latlng_to_cell(float(lat), float(lon), 9)
        per_cell[cell] = per_cell.get(cell, 0.0) + SEVERITY_KILLED * killed + SEVERITY_INJURED * injured
    return per_cell, str(lo), str(hi), len(rows)


def street_mile_cells(centerline_csv: Path) -> dict[str, float]:
    """Street miles per H3 r9 cell, sampled along each built street segment."""
    import geopandas as gpd
    import h3
    import pandas as pd
    from shapely import wkt

    frame = pd.read_csv(
        centerline_csv,
        usecols=["the_geom", "RW_TYPE", "STATUS"],
        dtype={"RW_TYPE": "Int64", "STATUS": "Int64"},
    )
    frame = frame[
        frame["RW_TYPE"].isin(STREET_RW_TYPES) & (frame["STATUS"] == BUILT_STATUS)
    ]
    geo = gpd.GeoSeries(frame["the_geom"].map(wkt.loads), crs=4326)
    # EPSG 2263 (NY state plane, feet) for true lengths.
    lengths_m = geo.to_crs(2263).length * 0.3048

    miles: dict[str, float] = {}
    for line, length_m in zip(geo, lengths_m):
        if length_m <= 0:
            continue
        n = max(2, int(length_m // SAMPLE_SPACING_M) + 1)
        share = (length_m / 1609.344) / n
        for i in range(n):
            point = line.interpolate(i / (n - 1), normalized=True)
            cell = h3.latlng_to_cell(point.y, point.x, 9)
            miles[cell] = miles.get(cell, 0.0) + share
    return miles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--ready-root", type=Path, default=REPO_ROOT / "data" / "ready")
    args = parser.parse_args()

    con = duckdb.connect()
    casualties, window_lo, window_hi, crash_rows = casualty_cells(
        con, args.raw_root / "safety" / "motor_vehicle_collisions.csv"
    )
    print(f"{crash_rows:,} casualty crashes {window_lo}..{window_hi} -> {len(casualties):,} cells")
    miles = street_mile_cells(args.raw_root / "transit" / "nyc_street_centerline.csv")
    print(f"street miles apportioned to {len(miles):,} cells")

    dropped_no_street = {
        cell: value for cell, value in casualties.items() if cell not in miles
    }
    scored_cells = sorted(miles)
    rate = np.array(
        [
            casualties.get(cell, 0.0) / max(miles[cell], MIN_MILES_DIVISOR)
            for cell in scored_cells
        ]
    )
    import pandas as pd

    scored = pd.DataFrame(
        {
            "h3_r9": scored_cells,
            # The rate under the conventional raw-value column name, so the
            # correlation and sensitivity tooling reads this table unmodified.
            "raw_count": rate,
            "casualties": [casualties.get(cell, 0.0) for cell in scored_cells],
            "street_miles": [round(miles[cell], 4) for cell in scored_cells],
        }
    )
    scored["score"] = percentile_score(scored["raw_count"], access_mode=False)

    out_dir = args.ready_root / "transit"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "transit_risk_scores_h3.parquet"
    con.execute("CREATE OR REPLACE TABLE t AS SELECT * FROM scored")
    con.execute(f"COPY t TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    manifest = {
        "generated": date.today().isoformat(),
        "window": [window_lo, window_hi],
        "casualty_crashes": crash_rows,
        "severity_weights": {"killed": SEVERITY_KILLED, "injured": SEVERITY_INJURED},
        "street_rw_types": list(STREET_RW_TYPES),
        "cells_scored": len(scored_cells),
        "casualty_cells_without_street": len(dropped_no_street),
        "casualty_severity_dropped": round(sum(dropped_no_street.values()), 1),
        "min_miles_divisor": MIN_MILES_DIVISOR,
        "upstream_note": "MVC updates paused upstream; window anchored to newest crash on file.",
    }
    (out_dir / "transit_risk.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
