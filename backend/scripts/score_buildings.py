#!/usr/bin/env python3
"""Score every NYC building footprint, using the backend's own algorithm.

The point of this pass is that the map should colour a building with the number
the analysis would actually report for it. So this does not invent a smoother
or prettier per-building metric -- it replays exactly what
``DirectQueryDataProvider._collect_prepared_scores`` does for a point, and
``compute_secondary_scores`` does to combine the results.

The spatial rule that decides the shape of this script and the map is:

    cells = r9 centroids whose haversine distance from the building is <= r
    sub_score = avg(score) over those cells

The candidate disk is topological, but the final inclusion test is metric and
depends on the building's exact coordinate. Two buildings in one r9 cell can
therefore include different edge cells. The bake must retain that distinction
or a building colour can disagree with its detail panel.

So:

  * safety / transit / amenities are computed per building coordinate from
    the same r9-centroid radius selection as the provider.
  * ``building`` uses the provider's separately clamped 250 m radius.

The broad H3 candidates and their centroids are cached per occupied cell; only
the final distance filter and aggregation run per building.

Run:
    python backend/scripts/score_buildings.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import duckdb
import h3
import pyarrow as pa

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from urban_dossier_backend.categories import CATEGORY_CONFIG  # noqa: E402
from urban_dossier_backend.metrics import METHODOLOGY_VERSION  # noqa: E402
from urban_dossier_backend.secondary_scoring import compute_secondary_scores  # noqa: E402
from urban_dossier_backend.utils import haversine_m  # noqa: E402

DEFAULT_BUILDINGS = Path("/mnt/data/urban-dossier-state/maps/buildings")
DEFAULT_READY = Path("/mnt/data/Urban-Dossier/data/ready")

# The radius the map's global view implies. The backend clamps the building
# category to [100, 250] separately; both are mirrored below.
DEFAULT_RADIUS_M = 500
BUILDING_RADIUS_M = 250
H3_R9_EDGE_M = float(h3.average_hexagon_edge_length(9, unit="m"))


NTA_PATH = Path("/mnt/data/Urban-Dossier/data/boundaries/nta_2020.geojson")
# Enough to keep the Hudson and East River piers, not enough to reach the far
# bank of the Kill van Kull. Verified against known points on both sides.
CITY_BUFFER_M = 100
SCORING_CONTRACT = "point-radius-haversine-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cells_for(
    latitude: float,
    longitude: float,
    cell_r9: str,
    radius_m: int,
    candidate_cache: dict[tuple[str, int], list[tuple[str, float, float]]],
) -> list[str]:
    """Mirror `DirectQueryDataProvider._h3_cells_for_radius` exactly."""
    center_spacing_m = H3_R9_EDGE_M * math.sqrt(3)
    k = max(1, math.ceil(radius_m / center_spacing_m) + 1)
    key = (cell_r9, k)
    candidates = candidate_cache.get(key)
    if candidates is None:
        candidates = [
            (candidate, *h3.cell_to_latlng(candidate))
            for candidate in h3.grid_disk(cell_r9, k)
        ]
        candidate_cache[key] = candidates
    inside = [
        candidate
        for candidate, cell_lat, cell_lon in candidates
        if haversine_m(latitude, longitude, cell_lat, cell_lon) <= radius_m
    ]
    return sorted(inside or [cell_r9])


def _ids_inside_nyc(con, index_path: Path) -> list[int] | None:
    """bldg_ids inside the city, or None if the boundary layer is unavailable."""
    if not NTA_PATH.exists():
        return None
    try:
        import geopandas as gpd
        from shapely import points
    except ImportError:
        return None

    nta = gpd.read_file(NTA_PATH)
    mask = (
        nta.geometry.union_all()
        .simplify(0.0001, preserve_topology=True)
        .buffer(CITY_BUFFER_M / 111320)
    )
    rows = con.execute(
        f"SELECT bldg_id, lon, lat FROM read_parquet('{index_path.as_posix()}')"
    ).fetchall()
    series = gpd.GeoSeries(points([[r[1], r[2]] for r in rows]), crs="EPSG:4326")
    inside = series.within(mask).to_numpy()
    return [rows[i][0] for i in range(len(rows)) if inside[i]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buildings-dir", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--ready-root", type=Path, default=DEFAULT_READY)
    parser.add_argument("--radius-m", type=int, default=DEFAULT_RADIUS_M)
    args = parser.parse_args()

    index_path = args.buildings_dir / "building_index.parquet"
    if not index_path.exists():
        print(
            f"error: {index_path} not found. Run extract_building_footprints.py first.",
            file=sys.stderr,
        )
        return 1

    t0 = time.time()
    con = duckdb.connect()

    # ---------------------------------------------------------------- cells --
    cells = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT h3_r9 FROM read_parquet('{index_path.as_posix()}') "
            "WHERE h3_r9 IS NOT NULL"
        ).fetchall()
    ]
    n_buildings = con.execute(
        f"SELECT count(*) FROM read_parquet('{index_path.as_posix()}')"
    ).fetchone()[0]
    print(f"{n_buildings:,} buildings across {len(cells):,} occupied r9 cells", flush=True)

    # ------------------------------------------------------- score tables ----
    # Loaded once into memory. Each is only a few thousand rows, and doing the
    # k-ring average in Python avoids issuing ~10^5 parameterised SQL queries.
    h3_tables: dict[str, dict[str, float]] = {}
    zip_tables: dict[str, dict[str, float]] = {}
    missing: list[str] = []
    for cfg in CATEGORY_CONFIG.values():
        for sub_cfg in cfg.get("sub_datasets", {}).values():
            rel = sub_cfg.get("score_table")
            if not rel:
                continue
            path = args.ready_root / rel
            if not path.exists():
                missing.append(rel)
                continue
            if sub_cfg.get("query_by") == "zip":
                rows = con.execute(
                    f"SELECT CAST(zip AS VARCHAR), try_cast(score AS DOUBLE) "
                    f"FROM read_parquet('{path.as_posix()}')"
                ).fetchall()
                zip_tables[rel] = {r[0]: r[1] for r in rows if r[1] is not None}
            else:
                rows = con.execute(
                    f"SELECT h3_r9, try_cast(score AS DOUBLE) "
                    f"FROM read_parquet('{path.as_posix()}')"
                ).fetchall()
                h3_tables[rel] = {r[0]: r[1] for r in rows if r[1] is not None}
    if missing:
        print(f"  note: {len(missing)} score tables absent, treated as no-data: {missing}")

    # ----------------------------------------------- per-building computation
    # Cache the broad H3 candidate ring per occupied cell. The exact distance
    # filter still uses each building coordinate, matching the live provider.
    candidate_cache: dict[tuple[str, int], list[tuple[str, float, float]]] = {}
    building_rows = con.execute(
        f"SELECT bldg_id, lat, lon, h3_r9 "
        f"FROM read_parquet('{index_path.as_posix()}') "
        "WHERE h3_r9 IS NOT NULL"
    ).fetchall()
    scored: dict[str, list] = {
        "bldg_id": [],
        "safety": [],
        "transit": [],
        "amenities": [],
        "building": [],
        "overall": [],
    }

    for i, (bldg_id, lat, lon, cell) in enumerate(building_rows):
        prepared: dict[str, dict[str, int | None]] = {}
        for category_id, cfg in CATEGORY_CONFIG.items():
            radius = (
                min(max(args.radius_m, 100), BUILDING_RADIUS_M)
                if category_id == "building"
                else args.radius_m
            )
            ring = _cells_for(
                float(lat),
                float(lon),
                cell,
                radius,
                candidate_cache,
            )

            sub_scores: dict[str, int | None] = {}
            for sub_name, sub_cfg in cfg.get("sub_datasets", {}).items():
                rel = sub_cfg.get("score_table")
                if not rel:
                    sub_scores[sub_name] = None
                    continue
                if sub_cfg.get("query_by") == "zip":
                    # ZIP-grained signals (EMS/fire response, parks) cannot be
                    # resolved from an H3 cell alone; they are filled in from
                    # the building's own ZIP in the join step below.
                    sub_scores[sub_name] = None
                    continue
                table = h3_tables.get(rel)
                if not table:
                    sub_scores[sub_name] = None
                    continue
                vals = [table[c] for c in ring if c in table]
                sub_scores[sub_name] = round(sum(vals) / len(vals)) if vals else None
            if any(v is not None for v in sub_scores.values()):
                prepared[category_id] = sub_scores

        scores = compute_secondary_scores(
            current_state={}, baselines={}, prepared_scores=prepared
        )
        scored["bldg_id"].append(bldg_id)
        for category_id in ("safety", "transit", "amenities", "building", "overall"):
            scored[category_id].append(scores.get(category_id))
        if (i + 1) % 100000 == 0:
            print(f"  scored {i + 1:,}/{len(building_rows):,} buildings", flush=True)

    con.register("baked_scores", pa.table(scored))
    print(f"per-building scoring done in {time.time() - t0:.1f}s", flush=True)

    # ------------------------------------------------------------- persist ---
    args.buildings_dir.mkdir(parents=True, exist_ok=True)
    # Buildings outside the city must not carry a score.
    #
    # The OSM extract is cut to a bounding box, so it includes Bayonne, Newark
    # and western Nassau County. Those have no NYC Open Data behind them, but an
    # r9 cell on the far bank of the Kill van Kull still picks up a score
    # through its k-ring and hands it to every building in it. 36,017 buildings
    # were scored this way -- a number about New Jersey derived entirely from
    # Staten Island.
    #
    # The mask is the NTA land union buffered by 100 m. Unbuffered it would cut
    # the Hudson and East River piers, which are genuinely NYC buildings sitting
    # past a shoreline drawn at the bulkhead; at 100 m the piers survive and the
    # far bank does not.
    keep_ids = _ids_inside_nyc(con, index_path)
    if keep_ids is not None:
        con.execute("CREATE TABLE in_city (bldg_id BIGINT)")
        con.executemany("INSERT INTO in_city VALUES (?)", [(i,) for i in keep_ids])
        city_filter = "JOIN in_city USING (bldg_id)"
        print(f"in-city buildings: {len(keep_ids):,}")
    else:
        city_filter = ""
        print("note: land mask unavailable, keeping all buildings")

    out_path = args.buildings_dir / "building_scores.parquet"
    con.execute(
        f"""
        COPY (
            SELECT
                b.bldg_id,
                b.lat,
                b.lon,
                b.h3_r8,
                b.h3_r9,
                s.safety,
                s.transit,
                s.amenities,
                s.building,
                s.overall
            FROM read_parquet('{index_path.as_posix()}') b
            {city_filter}
            LEFT JOIN baked_scores s USING (bldg_id)
        ) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )

    stats = con.execute(
        f"""
        SELECT
            count(*) AS n,
            count(overall) AS with_overall,
            count(DISTINCT overall) AS distinct_overall,
            min(overall), max(overall), round(avg(overall), 1)
        FROM read_parquet('{out_path.as_posix()}')
        """
    ).fetchone()

    # Colour domain per field, measured rather than assumed.
    #
    # These scores are not spread over 0-100. ``overall`` is a weighted average
    # of sub-scores, so it regresses hard to the middle: 96% of buildings land
    # between 34 and 68 on the validated snapshot. Painting that with a linear
    # 0-100 ramp spends two thirds of the colour range on values that do not
    # occur and renders the entire city as the ramp's midpoint -- which is
    # exactly what it did.
    #
    # Publishing the real 2nd/50th/98th percentiles lets the map stretch the
    # ramp over the data that exists and lets the legend say what its ends
    # mean, instead of claiming a range the data never reaches. Recomputed on
    # every run, so it tracks the data instead of ageing into a wrong constant.
    domains: dict[str, dict[str, float]] = {}
    for field in ("safety", "transit", "amenities", "building", "overall"):
        row = con.execute(
            f"""
            SELECT quantile_cont({field}, 0.02),
                   quantile_cont({field}, 0.50),
                   quantile_cont({field}, 0.98)
            FROM read_parquet('{out_path.as_posix()}')
            WHERE {field} IS NOT NULL
            """
        ).fetchone()
        if row and row[0] is None:
            continue
        low, mid, high = (round(float(v)) for v in row)
        # A degenerate domain would make the interpolation divide by zero.
        if high - low < 4:
            low, high = max(0, mid - 2), min(100, mid + 2)
        if not low < mid < high:
            mid = (low + high) / 2

        # The shape of the distribution, not just its ends. The map's legend
        # draws this instead of a plain gradient bar, because the ends alone
        # hide the thing that actually matters about these scores: they are
        # bunched. A reader looking at a 0-100 ramp assumes the city is spread
        # across it; showing the histogram says plainly that most of it is not.
        # Twenty five-point buckets across 0-100. Written as integer division
        # rather than width_bucket, which this DuckDB build does not have.
        buckets = con.execute(
            f"""
            SELECT least(19, greatest(0, ({field} / 5)::INTEGER)) AS b, count(*) AS n
            FROM read_parquet('{out_path.as_posix()}')
            WHERE {field} IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        counts = [0] * 20
        for bucket, n in buckets:
            counts[int(bucket)] += int(n)
        domains[field] = {
            "low": low,
            "mid": mid,
            "high": high,
            "histogram": counts,
        }

    manifest = {
        # Which version of the weights and definitions produced these scores.
        # The bake goes stale the moment metrics.py bumps this; consumers and
        # tests compare it rather than guessing from timestamps.
        "methodology_version": METHODOLOGY_VERSION,
        "scoring_contract": SCORING_CONTRACT,
        "artifact_sha256": _sha256(out_path),
        "building_index_sha256": _sha256(index_path),
        "source_score_sha256": {
            rel: _sha256(args.ready_root / rel)
            for rel in sorted(h3_tables)
        },
        "buildings": int(stats[0]),
        "with_overall_score": int(stats[1]),
        "distinct_overall_values": int(stats[2]),
        "overall_min": stats[3],
        "overall_max": stats[4],
        "overall_mean": stats[5],
        "colour_domains": domains,
        "colour_domain_note": (
            "2nd/50th/98th percentile of each field. The map stretches its "
            "ramp over this rather than over 0-100, because the scores do not "
            "span 0-100 and a linear full-range ramp paints everything the "
            "midpoint colour."
        ),
        "occupied_r9_cells": len(cells),
        "radius_m": args.radius_m,
        "building_radius_m": BUILDING_RADIUS_M,
        "grain_note": (
            "Source values vary at H3 r9 grain, but inclusion in the requested "
            "metric radius is filtered from each building coordinate exactly "
            "as in the backend point query."
        ),
        "missing_score_tables": missing,
        "elapsed_s": round(time.time() - t0, 1),
    }
    (args.buildings_dir / "building_scores.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
