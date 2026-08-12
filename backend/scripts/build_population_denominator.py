"""Allocate ACS tract population onto the H3 r9 grid -- EXPANSION_PLAN 1.5.

The correlation audit's blunt finding was that every count-based metric
correlates with every other at rho 0.2-0.6 because counts within a radius
measure activity density before they measure their own phenomenon. The cure
is a denominator, and this builds the first one: resident population per r9
cell, from the official ACS 2019-2023 5-year table B01003 (with MOE retained)
and the 2023 TIGER tract polygons.

Method -- documented because every choice moves numbers:

* Allocation is uniform *within* a tract: each tract's population is split
  equally across the r9 cells whose centres fall inside it. Centre-containment
  makes the assignment a clean partition (every cell belongs to exactly one
  tract), and equal split is the honest floor -- we do not pretend to know
  where inside a tract people live. Dasymetric refinement (building volumes
  are on disk already) is the documented upgrade, not smuggled in here.
* A tract too small to contain any cell centre donates its population to the
  cell containing the tract's own representative point, so no population is
  dropped. Conservation is asserted: allocated total must equal the ACS sum
  to the person.
* Water-heavy cells are not special-cased: a tract's shoreline cells receive
  their equal share. The land-fraction machinery downstream already exists to
  temper per-area readings; a second, different land correction here would
  double-apply it.
* Output is a *denominator artifact*, deliberately not a registry metric.
  Turning any existing count metric into a per-capita rate changes published
  scores, which is a methodology-version event with its own correlation and
  sensitivity run -- this file just makes that step possible.

Output: data/ready/context/population_r9.parquet
        (h3_r9, population, tract_geoid, tract_population, tract_moe)
        + population_r9.manifest.json

Usage:
    python backend/scripts/build_population_denominator.py
        [--acs-dat PATH] [--tiger-zip PATH] [--ready-root PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.metrics import METHODOLOGY_VERSION  # noqa: E402

NYC_COUNTIES = ("005", "047", "061", "081", "085")
DEFAULT_ACS = Path(
    "/mnt/data/urban-dossier-state/datasets/raw-expansion/acs/acsdt5y2023-b01003.dat"
)
DEFAULT_TIGER = Path(
    "/mnt/data/urban-dossier-state/datasets/raw/boundaries/tl_2023_36_tract.zip"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_population(acs_dat: Path) -> dict[str, tuple[int, int]]:
    """GEOID -> (population, MOE) for the five NYC counties."""
    out: dict[str, tuple[int, int]] = {}
    with acs_dat.open() as fh:
        header = fh.readline().strip().split("|")
        idx = {name: i for i, name in enumerate(header)}
        for line in fh:
            parts = line.rstrip("\n").split("|")
            geo_id = parts[idx["GEO_ID"]]
            if not geo_id.startswith("1400000US36"):
                continue
            fips = geo_id.removeprefix("1400000US")
            if fips[2:5] not in NYC_COUNTIES:
                continue
            estimate = parts[idx["B01003_E001"]]
            moe = parts[idx["B01003_M001"]]
            out[fips] = (int(estimate or 0), int(moe or 0))
    return out


def tract_cells(geometry) -> list[str]:
    """r9 cells whose centres fall inside the geometry (clean partition)."""
    import h3

    geoms = getattr(geometry, "geoms", [geometry])
    cells: set[str] = set()
    for geom in geoms:
        shape = h3.geo_to_h3shape(geom.__geo_interface__)
        cells.update(h3.h3shape_to_cells(shape, 9))
    return sorted(cells)


def allocate(
    populations: dict[str, tuple[int, int]], tracts
) -> tuple[dict[str, float], dict[str, str], int]:
    """Split each tract's population equally over its cells.

    Returns (cell -> population, cell -> owning tract, centre-fallback count).
    Cells keep fractional people on purpose: rounding per cell would break
    conservation, and a denominator has no need to be an integer.
    """
    import h3

    cell_population: dict[str, float] = {}
    cell_tract: dict[str, str] = {}
    fallbacks = 0
    for _, row in tracts.iterrows():
        fips = row["GEOID"]
        population, _ = populations.get(fips, (0, 0))
        cells = tract_cells(row["geometry"])
        if not cells:
            point = row["geometry"].representative_point()
            cells = [h3.latlng_to_cell(point.y, point.x, 9)]
            fallbacks += 1
        share = population / len(cells)
        for cell in cells:
            cell_population[cell] = cell_population.get(cell, 0.0) + share
            # Last writer wins for provenance; only fallback donations overlap.
            cell_tract.setdefault(cell, fips)
    return cell_population, cell_tract, fallbacks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--acs-dat", type=Path, default=DEFAULT_ACS)
    parser.add_argument("--tiger-zip", type=Path, default=DEFAULT_TIGER)
    parser.add_argument("--ready-root", type=Path, default=REPO_ROOT / "data" / "ready")
    args = parser.parse_args()

    import duckdb
    import geopandas as gpd

    populations = load_population(args.acs_dat)
    acs_total = sum(p for p, _ in populations.values())
    print(f"{len(populations):,} NYC tracts, ACS population {acs_total:,}")

    tracts = gpd.read_file(f"zip://{args.tiger_zip.as_posix()}")
    tracts = tracts[tracts["COUNTYFP"].isin(NYC_COUNTIES)].to_crs(4326)
    print(f"{len(tracts):,} tract polygons")

    cell_population, cell_tract, fallbacks = allocate(populations, tracts)
    allocated = sum(cell_population.values())
    drift = abs(allocated - acs_total)
    if drift > 1e-6 * max(acs_total, 1):
        raise SystemExit(
            f"conservation violated: allocated {allocated:,.2f} vs ACS {acs_total:,}"
        )

    import pandas as pd

    frame = pd.DataFrame(
        {
            "h3_r9": sorted(cell_population),
            "population": [round(cell_population[c], 3) for c in sorted(cell_population)],
            "tract_geoid": [cell_tract[c] for c in sorted(cell_population)],
        }
    )
    frame["tract_population"] = frame["tract_geoid"].map(lambda f: populations[f][0])
    frame["tract_moe"] = frame["tract_geoid"].map(lambda f: populations[f][1])

    out_dir = args.ready_root / "context"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "population_r9.parquet"
    con = duckdb.connect()
    con.execute("CREATE OR REPLACE TABLE t AS SELECT * FROM frame")
    con.execute(f"COPY t TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    manifest = {
        "methodology_version": METHODOLOGY_VERSION,
        "generated": date.today().isoformat(),
        "source": {
            "population": "ACS 2019-2023 5-year B01003 (table-based summary file)",
            "geometry": "TIGER/Line 2023 census tracts, NY",
        },
        "allocation": "equal split over r9 cells by centre containment; "
                      "representative-point fallback for sub-cell tracts",
        "tracts": len(populations),
        "cells": len(frame),
        "acs_population": acs_total,
        "allocated_population": round(allocated, 2),
        "centre_fallback_tracts": fallbacks,
        "role": "denominator artifact -- not a scored metric; per-capita "
                "conversion of any metric is a methodology-version event",
    }
    (out_dir / "population_r9.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
