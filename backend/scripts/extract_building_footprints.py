#!/usr/bin/env python3
"""Extract NYC building footprints from the local OSM extract.

Why OSM rather than the city's own Building Footprints release: this project is
local-first, and ``NewYork.osm.pbf`` is already on disk for the walking graph.
Downloading a second 1.1M-polygon dataset would add a network dependency and a
second geometry lineage for no gain -- the per-building scores computed
downstream need a footprint and a centroid, not a BIN. The one thing OSM cannot
give us is the city's BBL key, so the building-condition category is joined
separately in ``score_buildings.py`` by locating each BBL point inside a
footprint.

Output is a Parquet table of footprints with a stable integer id, the centroid,
and the H3 cells the centroid falls in at r8 and r9 -- the same grains the
deterministic scoring already uses, so the join downstream is a hash lookup
rather than a spatial query.

Run:
    python backend/scripts/extract_building_footprints.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_PBF = Path("/mnt/data/urban-dossier-state/maps/source/NewYork.osm.pbf")
DEFAULT_OUT = Path("/mnt/data/urban-dossier-state/maps/buildings")

# NYC bounding box. The extract is a US-Northeast cut and reaches well past the
# five boroughs; scoring anything outside them would burn time on buildings the
# datasets have no coverage for.
NYC_BBOX = (-74.30, 40.47, -73.68, 40.93)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=DEFAULT_PBF)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.pbf.exists():
        print(f"error: OSM extract not found at {args.pbf}", file=sys.stderr)
        return 1

    import h3
    import pyrosm
    from shapely.geometry import box

    args.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    print(f"reading {args.pbf} (bbox-filtered to NYC)...", flush=True)
    # pyrosm wants a Shapely geometry or a list, not a tuple.
    osm = pyrosm.OSM(str(args.pbf), bounding_box=box(*NYC_BBOX))
    gdf = osm.get_buildings()
    if gdf is None or gdf.empty:
        print("error: no buildings returned from the extract", file=sys.stderr)
        return 1
    print(f"  {len(gdf):,} raw building features in {time.time() - t0:.1f}s", flush=True)

    # Keep polygonal footprints only. Relations occasionally come through as
    # lines or points and cannot be filled on a map.
    gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    gdf = gdf[gdf.geometry.notna() & gdf.geometry.is_valid]
    print(f"  {len(gdf):,} valid polygons", flush=True)

    # Representative point, not centroid: for an L-shaped or ring-shaped
    # footprint the centroid can land outside the building, which would put it
    # in the wrong H3 cell.
    reps = gdf.geometry.representative_point()
    gdf["lon"] = reps.x.astype("float64")
    gdf["lat"] = reps.y.astype("float64")

    gdf["h3_r8"] = [
        h3.latlng_to_cell(lat, lon, 8)
        for lat, lon in zip(gdf["lat"], gdf["lon"], strict=True)
    ]
    gdf["h3_r9"] = [
        h3.latlng_to_cell(lat, lon, 9)
        for lat, lon in zip(gdf["lat"], gdf["lon"], strict=True)
    ]
    gdf["bldg_id"] = range(len(gdf))

    keep = ["bldg_id", "lat", "lon", "h3_r8", "h3_r9", "geometry"]
    for optional in ("building", "height", "building:levels", "name"):
        if optional in gdf.columns:
            keep.append(optional)
    gdf = gdf[keep]

    geo_path = args.out_dir / "building_footprints.parquet"
    gdf.to_parquet(geo_path, index=False)

    # A geometry-free copy so the scoring pass can use DuckDB without pulling
    # 1M polygons through GeoPandas again.
    attrs = gdf.drop(columns=["geometry"])
    attr_path = args.out_dir / "building_index.parquet"
    attrs.to_parquet(attr_path, index=False)

    manifest = {
        "source_pbf": str(args.pbf),
        "source_pbf_mtime": os.path.getmtime(args.pbf),
        "bbox": NYC_BBOX,
        "building_count": int(len(gdf)),
        "distinct_h3_r8": int(attrs["h3_r8"].nunique()),
        "distinct_h3_r9": int(attrs["h3_r9"].nunique()),
        "elapsed_s": round(time.time() - t0, 1),
    }
    (args.out_dir / "buildings.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print(json.dumps(manifest, indent=2))
    print(f"\nwrote {geo_path}")
    print(f"wrote {attr_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
