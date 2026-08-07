#!/usr/bin/env python3
"""Bake per-building scores into their own vector tileset.

The map used to colour buildings in JavaScript: on every camera move it pulled
every building out of the basemap tiles, computed a centroid, ran a
point-in-polygon against the overlay, and rebuilt a GeoJSON source. That is the
cost that made a hexagon overlay the practical choice in the first place, and it
is per-frame work on the main thread -- the GPU was never the constraint.

Baking the scores into the tiles moves that work to build time, once. The map
then colours buildings with a paint expression reading a feature property, which
is exactly what MapLibre's GPU path is for: no querySourceFeatures, no
setData, no JS per frame.

Emits a separate tileset rather than rewriting the basemap so the basemap stays
a pristine OpenMapTiles artefact that can be regenerated independently.

Run:
    python backend/scripts/build_building_tiles.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BUILDINGS = Path("/mnt/data/urban-dossier-state/maps/buildings")
DEFAULT_OUT = Path("/mnt/data/urban-dossier-state/maps/output/building-scores.mbtiles")

# The basemap's building layer exists at z13-14 and the tileset stops at 14, so
# there is no building geometry below 13 no matter what we do here. Matching
# that floor keeps the two sources consistent; going to 16 gives MapLibre real
# geometry to work with when the user zooms past the basemap's own maximum.
MIN_ZOOM = 13
MAX_ZOOM = 16


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--buildings-dir", type=Path, default=DEFAULT_BUILDINGS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--keep-geojson", action="store_true")
    args = parser.parse_args()

    if shutil.which("tippecanoe") is None:
        print("error: tippecanoe not on PATH (apt-get install tippecanoe)", file=sys.stderr)
        return 1

    geo_path = args.buildings_dir / "building_footprints.parquet"
    score_path = args.buildings_dir / "building_scores.parquet"
    for p in (geo_path, score_path):
        if not p.exists():
            print(f"error: {p} not found; run the extract and score passes first", file=sys.stderr)
            return 1

    import geopandas as gpd
    import pandas as pd

    t0 = time.time()
    print("loading footprints + scores...", flush=True)
    gdf = gpd.read_parquet(geo_path, columns=["bldg_id", "geometry"])
    scores = pd.read_parquet(
        score_path,
        columns=["bldg_id", "safety", "transit", "amenities", "building", "overall"],
    )
    gdf = gdf.merge(scores, on="bldg_id", how="inner")
    print(f"  {len(gdf):,} buildings with scores", flush=True)

    # Drop buildings with no score at all rather than shipping grey geometry:
    # the basemap already draws unscored buildings, and a second silent copy of
    # them would double the tile size for nothing.
    before = len(gdf)
    gdf = gdf[gdf["overall"].notna()].copy()
    print(f"  {before - len(gdf):,} dropped for having no score", flush=True)

    # Small integer columns keep the tiles compact; MapLibre compares numbers
    # in paint expressions, so these must not become strings.
    for col in ("safety", "transit", "amenities", "building", "overall"):
        gdf[col] = gdf[col].astype("Int16")

    tmp_geojson = args.buildings_dir / "_building_scores.geojsons"
    print(f"writing {tmp_geojson}...", flush=True)
    gdf.to_file(tmp_geojson, driver="GeoJSONSeq")
    size_mb = tmp_geojson.stat().st_size / 1e6
    print(f"  {size_mb:.1f} MB", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "tippecanoe",
        "-o", str(args.out),
        "--force",
        "-l", "building_scores",
        f"--minimum-zoom={MIN_ZOOM}",
        f"--maximum-zoom={MAX_ZOOM}",
        # Every building must survive to the maximum zoom -- a dropped building
        # is a hole in the choropleth, not a decluttered label.
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        str(tmp_geojson),
    ]
    print("running:", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-3000:], file=sys.stderr)
        print(proc.stderr[-3000:], file=sys.stderr)
        return proc.returncode
    print(proc.stderr[-1500:] or proc.stdout[-1500:], flush=True)

    if not args.keep_geojson:
        tmp_geojson.unlink(missing_ok=True)

    manifest = {
        "tileset": str(args.out),
        "layer": "building_scores",
        "buildings": int(len(gdf)),
        "minzoom": MIN_ZOOM,
        "maxzoom": MAX_ZOOM,
        "size_mb": round(args.out.stat().st_size / 1e6, 1),
        "elapsed_s": round(time.time() - t0, 1),
    }
    (args.buildings_dir / "building_tiles.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
