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
) -> tuple[list[tuple[str, str, float, float]], int]:
    """Split each tract's population equally over its cells.

    Returns the full contribution edge list -- (cell, tract, population
    share, MOE share) -- and the centre-fallback count. The first version
    collapsed provenance to one tract per cell with ``setdefault``, which
    silently swallowed 55 tracts' identities wherever a fallback donation
    landed on an occupied cell, and made per-cell MOE columns describe the
    wrong tract (review finding). Keeping every edge costs a few hundred KB
    and makes provenance a fact instead of a casualty.

    MOE shares scale with the population share (tract_moe x share/tract_pop),
    the standard ACS proportion approximation; per-cell MOE aggregates as the
    root-sum-square of its edges. Cells keep fractional people on purpose:
    rounding per cell would break conservation.
    """
    import h3

    edges: list[tuple[str, str, float, float]] = []
    fallbacks = 0
    for _, row in tracts.iterrows():
        fips = row["GEOID"]
        population, moe = populations.get(fips, (0, 0))
        cells = tract_cells(row["geometry"])
        if not cells:
            point = row["geometry"].representative_point()
            cells = [h3.latlng_to_cell(point.y, point.x, 9)]
            fallbacks += 1
        share = population / len(cells)
        moe_share = (moe / len(cells)) if population else 0.0
        for cell in cells:
            edges.append((cell, fips, share, moe_share))
    return edges, fallbacks


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

    edges, fallbacks = allocate(populations, tracts)
    allocated = sum(share for _, _, share, _ in edges)
    drift = abs(allocated - acs_total)
    if drift > 1e-6 * max(acs_total, 1):
        raise SystemExit(
            f"conservation violated: allocated {allocated:,.2f} vs ACS {acs_total:,}"
        )

    import math

    import pandas as pd

    edge_frame = pd.DataFrame(
        edges, columns=["h3_r9", "tract_geoid", "population_share", "moe_share"]
    )
    grouped = edge_frame.groupby("h3_r9")
    frame = pd.DataFrame(
        {
            # Full precision: rounding to 3 dp cost 0.099 people citywide --
            # under one person, but "exact" was the published claim, so exact
            # it is, verified by re-reading the artifact below.
            "population": grouped["population_share"].sum(),
            "population_moe": grouped["moe_share"].apply(
                lambda shares: math.sqrt(float((shares ** 2).sum()))
            ),
            "tract_count": grouped["tract_geoid"].nunique(),
            # The largest contributor, for display; the edge-list artifact is
            # the authoritative provenance.
            "primary_tract_geoid": edge_frame.sort_values("population_share")
            .groupby("h3_r9")["tract_geoid"].last(),
        }
    ).reset_index()

    out_dir = args.ready_root / "context"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "population_r9.parquet"
    prov_out = out_dir / "population_r9_provenance.parquet"
    con = duckdb.connect()
    for target, table in ((out, "frame"), (prov_out, "edge_frame")):
        tmp = target.with_suffix(".parquet.tmp")
        con.execute(f"CREATE OR REPLACE TABLE t AS SELECT * FROM {table}")
        con.execute(f"COPY t TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        tmp.replace(target)  # atomic within the filesystem

    # Trust the artifact, not the intention: re-read what was written.
    reread_total, reread_rows = con.execute(
        f"SELECT sum(population), count(*) FROM read_parquet('{out.as_posix()}')"
    ).fetchone()
    if abs(reread_total - acs_total) > 1e-6 * acs_total:
        raise SystemExit(
            f"artifact re-read total {reread_total:,.4f} != ACS {acs_total:,}"
        )
    prov_tracts = con.execute(
        f"SELECT count(DISTINCT tract_geoid) FROM read_parquet('{prov_out.as_posix()}')"
    ).fetchone()[0]

    def sha256(path: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    manifest = {
        "methodology_version": METHODOLOGY_VERSION,
        "generated": date.today().isoformat(),
        "source": {
            "population": "ACS 2019-2023 5-year B01003 (table-based summary file)",
            "population_sha256": sha256(args.acs_dat),
            "geometry": "TIGER/Line 2023 census tracts, NY",
            "geometry_sha256": sha256(args.tiger_zip),
        },
        "allocation": "equal split over r9 cells by centre containment; "
                      "representative-point fallback for sub-cell tracts; "
                      "full contribution edges in the provenance artifact",
        "tracts": len(populations),
        "tracts_in_provenance": int(prov_tracts),
        "cells": int(reread_rows),
        "acs_population": acs_total,
        "allocated_population_reread": float(reread_total),
        "centre_fallback_tracts": fallbacks,
        "artifacts": {
            out.name: {"sha256": sha256(out), "bytes": out.stat().st_size,
                        "rows": int(reread_rows)},
            prov_out.name: {"sha256": sha256(prov_out), "bytes": prov_out.stat().st_size,
                             "rows": len(edge_frame)},
        },
        "schema": {
            out.name: ["h3_r9", "population", "population_moe", "tract_count",
                        "primary_tract_geoid"],
            prov_out.name: ["h3_r9", "tract_geoid", "population_share", "moe_share"],
        },
        "role": "denominator artifact -- not a scored metric; per-capita "
                "conversion of any metric is a methodology-version event",
    }
    manifest_path = out_dir / "population_r9.manifest.json"
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(manifest_path)
    print(f"wrote {out} + {prov_out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
