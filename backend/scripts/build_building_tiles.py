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

# Two passes joined into one tileset, because the flat and the 3D views want
# different things from the same buildings.
#
# The choropleth only needs z13 up: the basemap has no building geometry below
# that, so there is nothing to colour. The 3D view needs the skyline while the
# whole city is on screen, which is z10, and 1.09M prisms in the four tiles
# that cover NYC at z10 is not something to hand a browser.
#
# The split is cheap because NYC's heights are extremely skewed -- a median of
# 7.9 m, but 3,006 buildings over 50 m. A city-scale massing model costs ten
# thousand prisms, not a million, and a 7.9 m rowhouse is sub-pixel at z10
# anyway, so dropping it loses nothing a viewer could have seen.
#
# Implemented as two tippecanoe runs rather than per-feature `tippecanoe`
# minzoom blocks: tippecanoe 2.49 accepts those blocks and then emits nearly
# empty tiles (a 50k-feature sample went from 6.12 MB to 0.36 MB, and the same
# tile fell from 2,860 to 188 bytes). Both passes below use only behaviour that
# is verified in the manifest counts.
DETAIL_LAYER = "building_scores"
DETAIL_MIN_ZOOM = 13
MASSING_LAYER = "building_massing"
MASSING_MIN_ZOOM = 10
MASSING_MAX_ZOOM = 12
MASSING_MIN_HEIGHT_M = 25.0
MAX_ZOOM = 16

# Fallbacks for the 28% of footprints with no height tag. 3.5 m per storey is
# the usual OSM convention; the flat default is close to the measured median so
# an unknown building sits with its neighbours instead of standing out.
METRES_PER_LEVEL = 3.5
DEFAULT_HEIGHT_M = 8.0
# One World Trade is 541 m. Anything past that is a units error or a typo, of
# which the extract has ten.
MAX_PLAUSIBLE_HEIGHT_M = 550.0


def _write_geojsonseq(path: Path, gdf, shapely) -> int:
    """Write newline-delimited GeoJSON. Returns the feature count.

    Hand-rolled rather than GeoPandas' GeoJSONSeq writer so the numeric columns
    keep their types exactly; shapely.to_geojson vectorises the expensive part,
    leaving string assembly.
    """
    geom_json = shapely.to_geojson(gdf.geometry.to_numpy())
    cols = {
        name: gdf[name].to_numpy()
        for name in (
            "safety", "transit", "amenities", "building", "overall",
            "height_m", "height_known",
        )
    }

    def _score(value):
        return "null" if value is None or value != value else int(value)

    with path.open("w", encoding="utf-8") as fh:
        for i, geom in enumerate(geom_json):
            fh.write(
                '{"type":"Feature","properties":{'
                '"safety":%s,"transit":%s,"amenities":%s,"building":%s,'
                '"overall":%s,"height":%.1f,"height_known":%s},"geometry":%s}\n'
                % (
                    _score(cols["safety"][i]),
                    _score(cols["transit"][i]),
                    _score(cols["amenities"][i]),
                    _score(cols["building"][i]),
                    _score(cols["overall"][i]),
                    float(cols["height_m"][i]),
                    "true" if cols["height_known"][i] else "false",
                    geom,
                )
            )
    return len(geom_json)


def _run_tippecanoe(src: Path, out: Path, layer: str, minzoom: int, maxzoom: int) -> None:
    cmd = [
        "tippecanoe",
        "-o", str(out),
        "--force",
        "-l", layer,
        f"--minimum-zoom={minzoom}",
        f"--maximum-zoom={maxzoom}",
        # Every building must survive -- a dropped building is a hole in the
        # choropleth, not a decluttered label. Thinning is done by splitting the
        # input into two passes by height, not by letting tippecanoe drop by
        # density, which would thin exactly where the city is most built up.
        "--no-feature-limit",
        "--no-tile-size-limit",
        str(src),
    ]
    print("running:", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:], file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(proc.returncode)


def _tile_stats(path: Path) -> dict[str, Any]:
    import sqlite3

    con = sqlite3.connect(path)
    try:
        return {
            f"z{z}": {"tiles": n, "mb": round(b / 1e6, 2)}
            for z, n, b in con.execute(
                "SELECT zoom_level, count(*), sum(length(tile_data)) "
                "FROM tiles GROUP BY 1 ORDER BY 1"
            )
        }
    finally:
        con.close()


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
    import numpy as np
    import pandas as pd
    import shapely

    t0 = time.time()
    print("loading footprints + scores...", flush=True)
    gdf = gpd.read_parquet(
        geo_path, columns=["bldg_id", "geometry", "height", "building:levels"]
    )
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

    # --- heights -----------------------------------------------------------
    # OSM height first, storeys second, a flat default last. ``height_known``
    # travels with the feature so the view can tell a measured tower from a
    # guessed one rather than presenting both as fact.
    raw_h = pd.to_numeric(gdf["height"], errors="coerce")
    raw_h = raw_h.where((raw_h > 0) & (raw_h <= MAX_PLAUSIBLE_HEIGHT_M))
    levels = pd.to_numeric(gdf["building:levels"], errors="coerce")
    from_levels = (levels * METRES_PER_LEVEL).where((levels > 0) & (levels <= 150))
    height = raw_h.fillna(from_levels)
    gdf["height_known"] = height.notna()
    gdf["height_m"] = height.fillna(DEFAULT_HEIGHT_M).round(1)
    print(
        f"  heights: {int(raw_h.notna().sum()):,} measured, "
        f"{int(from_levels.notna().sum() - (raw_h.notna() & from_levels.notna()).sum()):,} "
        f"from storeys, {int((~gdf['height_known']).sum()):,} defaulted",
        flush=True,
    )

    # Small integer columns keep the tiles compact; MapLibre compares numbers
    # in paint expressions, so these must not become strings.
    for col in ("safety", "transit", "amenities", "building", "overall"):
        gdf[col] = gdf[col].astype("Int16")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    work = args.buildings_dir

    # --- pass 1: every building, z13-16 -------------------------------------
    detail_src = work / "_building_scores.geojsons"
    print(f"writing {detail_src}...", flush=True)
    n_detail = _write_geojsonseq(detail_src, gdf, shapely)
    print(f"  {n_detail:,} features, {detail_src.stat().st_size / 1e6:.1f} MB", flush=True)
    detail_mb = work / "_detail.mbtiles"
    _run_tippecanoe(detail_src, detail_mb, DETAIL_LAYER, DETAIL_MIN_ZOOM, MAX_ZOOM)

    # --- pass 2: the massing model, z10-12 ----------------------------------
    tall = gdf[gdf["height_m"] >= MASSING_MIN_HEIGHT_M].copy()
    massing_src = work / "_building_massing.geojsons"
    n_massing = _write_geojsonseq(massing_src, tall, shapely)
    print(f"massing: {n_massing:,} buildings >= {MASSING_MIN_HEIGHT_M:.0f} m", flush=True)
    massing_mb = work / "_massing.mbtiles"
    _run_tippecanoe(
        massing_src, massing_mb, MASSING_LAYER, MASSING_MIN_ZOOM, MASSING_MAX_ZOOM
    )

    # --- join ---------------------------------------------------------------
    # Two layers, disjoint zoom ranges, one file so the client needs one source.
    join = ["tile-join", "-f", "-o", str(args.out), str(massing_mb), str(detail_mb)]
    print("running:", " ".join(join), flush=True)
    proc = subprocess.run(join, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:], file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        return proc.returncode

    if not args.keep_geojson:
        for tmp in (detail_src, massing_src, detail_mb, massing_mb):
            tmp.unlink(missing_ok=True)

    stats = _tile_stats(args.out)
    manifest = {
        "tileset": str(args.out),
        "layers": {
            DETAIL_LAYER: {
                "buildings": int(n_detail),
                "minzoom": DETAIL_MIN_ZOOM,
                "maxzoom": MAX_ZOOM,
            },
            MASSING_LAYER: {
                "buildings": int(n_massing),
                "min_height_m": MASSING_MIN_HEIGHT_M,
                "minzoom": MASSING_MIN_ZOOM,
                "maxzoom": MASSING_MAX_ZOOM,
            },
        },
        "tiles_by_zoom": stats,
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
