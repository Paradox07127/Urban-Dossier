#!/usr/bin/env python3
"""Build a terrain tileset that lifts New York City onto a plateau.

The 3D view wants two things that pulled against each other: a model with a
visible cut edge, and a map printed on its surface. A raised slab gave the
first and made the second impossible -- fill-extrusion is the only layer type
MapLibre can place at an elevation (the renderer refuses a negative base
outright), and symbols cannot be lifted at all, so a raised ground meant no
street names anywhere.

Terrain resolves it. MapLibre drapes the whole basemap over terrain, labels
included, and places extrusions on it. So instead of building a slab out of
geometry, this encodes one as elevation: the five boroughs sit at a constant
height and everything beyond the shoreline sits at zero. The step between them
is the cut edge, and it comes out of the same coastline the overview cells are
clipped against, so the model's edge and the data's edge stay one line.

The elevation is a fiction and deliberately flat -- this is not real terrain,
it is a plinth. NYC's actual relief tops out around 125 m and would fight the
building heights for legibility.

Output is Terrain-RGB, which is what MapLibre's "mapbox" encoding expects:

    height = -10000 + (R * 65536 + G * 256 + B) * 0.1

Run:
    python backend/scripts/build_plateau_dem.py
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path

DEFAULT_NTA = Path("/mnt/data/Urban-Dossier/data/boundaries/nta_2020.geojson")
DEFAULT_OUT = Path("/mnt/data/urban-dossier-state/maps/output/nyc-plateau-dem.mbtiles")

# How high the model sits above the surrounding water.
#
# Read against the buildings standing on it: tall enough that the cut edge is
# unmistakable at city zoom, short enough that it does not dwarf a 400 m tower
# when you come in close.
PLATEAU_M = 260.0
SEA_M = 0.0

# z10 is where the whole city fits; past z14 the coastline is already finer
# than the eye can use and each extra level quadruples the tile count.
MIN_ZOOM = 8
MAX_ZOOM = 13

NYC_BBOX = (-74.30, 40.47, -73.68, 40.93)


def terrain_rgb(metres: float) -> tuple[int, int, int]:
    v = round((metres + 10000) / 0.1)
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def lnglat_to_tile_px(lng: float, lat: float, zoom: int, size: int) -> tuple[float, float]:
    """Global pixel coordinates at this zoom."""
    n = size * (2 ** zoom)
    x = (lng + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nta", type=Path, default=DEFAULT_NTA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tile-size", type=int, default=256)
    args = parser.parse_args()

    if not args.nta.exists():
        print(f"error: {args.nta} not found")
        return 1

    import geopandas as gpd
    from PIL import Image, ImageDraw
    from shapely.geometry import box

    t0 = time.time()
    nta = gpd.read_file(args.nta)
    # Simplified hard, because the DEM's own resolution is the real limit: one
    # pixel is about 5 m at z13, so shoreline detail below that cannot survive
    # rasterising and only costs time in the polygon transform.
    land = nta.geometry.union_all().simplify(0.00005, preserve_topology=True)
    polys = list(getattr(land, "geoms", [land]))
    print(f"land: {len(polys)} polygons", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        args.out.unlink()
    con = sqlite3.connect(args.out)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute(
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB)"
    )

    size = args.tile_size
    sea_rgb = terrain_rgb(SEA_M)
    land_rgb = terrain_rgb(PLATEAU_M)
    print(f"sea {SEA_M} m -> rgb{sea_rgb};  plateau {PLATEAU_M} m -> rgb{land_rgb}")

    written = 0
    import io

    for zoom in range(MIN_ZOOM, MAX_ZOOM + 1):
        x0, y1 = lnglat_to_tile_px(NYC_BBOX[0], NYC_BBOX[1], zoom, size)
        x1, y0 = lnglat_to_tile_px(NYC_BBOX[2], NYC_BBOX[3], zoom, size)
        tx0, tx1 = int(x0 // size), int(x1 // size)
        ty0, ty1 = int(y0 // size), int(y1 // size)

        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                # Which part of the land falls in this tile?
                west = tx * size / (size * 2 ** zoom) * 360.0 - 180.0
                east = (tx + 1) * size / (size * 2 ** zoom) * 360.0 - 180.0

                def _lat(py: float) -> float:
                    n = math.pi - 2 * math.pi * py / (2 ** zoom)
                    return math.degrees(math.atan(math.sinh(n)))

                north, south = _lat(ty), _lat(ty + 1)
                tile_box = box(west, south, east, north)
                if not tile_box.intersects(land):
                    continue

                img = Image.new("RGB", (size, size), sea_rgb)
                if tile_box.within(land):
                    img.paste(land_rgb, (0, 0, size, size))
                else:
                    draw = ImageDraw.Draw(img)
                    ox, oy = tx * size, ty * size
                    for poly in polys:
                        if not poly.intersects(tile_box):
                            continue
                        clipped = poly.intersection(tile_box.buffer(0.002))
                        for part in getattr(clipped, "geoms", [clipped]):
                            if part.is_empty or part.geom_type != "Polygon":
                                continue
                            pts = [
                                tuple(v - o for v, o in
                                      zip(lnglat_to_tile_px(lng, lat, zoom, size), (ox, oy)))
                                for lng, lat in part.exterior.coords
                            ]
                            if len(pts) >= 3:
                                draw.polygon(pts, fill=land_rgb)
                            # Holes in the land -- inland water the plateau
                            # should not cover -- go back to sea level.
                            for ring in part.interiors:
                                hp = [
                                    tuple(v - o for v, o in
                                          zip(lnglat_to_tile_px(lng, lat, zoom, size), (ox, oy)))
                                    for lng, lat in ring.coords
                                ]
                                if len(hp) >= 3:
                                    draw.polygon(hp, fill=sea_rgb)

                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                flipped = (1 << zoom) - 1 - ty
                con.execute(
                    "INSERT INTO tiles VALUES (?, ?, ?, ?)",
                    (zoom, tx, flipped, buf.getvalue()),
                )
                written += 1
        print(f"  z{zoom}: {written} tiles so far", flush=True)

    con.execute(
        "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)"
    )
    for name, value in [
        ("name", "NYC plateau DEM"),
        ("format", "png"),
        ("type", "baselayer"),
        ("version", "1"),
        ("minzoom", str(MIN_ZOOM)),
        ("maxzoom", str(MAX_ZOOM)),
        ("bounds", ",".join(str(v) for v in NYC_BBOX)),
        ("description", f"Terrain-RGB plinth: {PLATEAU_M} m over NYC land, 0 elsewhere"),
    ]:
        con.execute("INSERT INTO metadata VALUES (?, ?)", (name, value))
    con.commit()
    con.close()

    manifest = {
        "tileset": str(args.out),
        "plateau_m": PLATEAU_M,
        "minzoom": MIN_ZOOM,
        "maxzoom": MAX_ZOOM,
        "tiles": written,
        "size_mb": round(args.out.stat().st_size / 1e6, 2),
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
