"""Overview cells must be the true H3 cell, clipped to dry land.

Two regressions are pinned here.

*Shrunken cells.* When the backend supplied only a centre point, the Node proxy
invented a hexagon around it with a hardcoded 0.0025-degree radius. Against an
r8 cell's ~0.0048 degrees that painted 27% of each cell's area and left the
other 73% as bare basemap, so a grid that tiles the city with no holes looked
like scattered dots.

*Scores over water.* The grid does not know where the city ends. Cells reach
4.7 km offshore, and a cell over open water still gets a score -- 170 amenities
cells sat off land and 134 of them fell below 40, painting the East River and
Jamaica Bay the same red as an actually underserved block. There are no bodegas
in the harbour because nobody lives there, and a reader cannot tell that from
the colour.
"""

from __future__ import annotations

import math

import h3
import pytest

from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider

# Times Square. Far enough from any shoreline that its whole r8 cell is land,
# so it must come back as an untouched hexagon.
INLAND_CELL = h3.latlng_to_cell(40.7580, -73.9855, 8)
# Mid-Hudson between Manhattan and Weehawken: no land at all.
OPEN_WATER_CELL = h3.latlng_to_cell(40.7600, -74.0120, 8)


def _attach(cells):
    return DirectQueryDataProvider._attach_cell_boundaries(
        [{"h3": c, "overall_score": 50} for c in cells]
    )


def _rings(row):
    """Every exterior ring in a row's boundary, whatever its geometry type."""
    coords = row["boundary"]
    if row.get("boundary_type") == "MultiPolygon":
        return [poly[0] for poly in coords]
    return [coords[0]]


def test_boundary_is_wellformed_geojson_for_its_declared_type():
    """One declared type must mean one shape.

    An earlier version returned a bare ring when nothing was clipped and proper
    Polygon coordinates when something was, both labelled "Polygon", so every
    consumer had to sniff the nesting depth to tell them apart.
    """
    from shapely.geometry import shape

    coastal = list(h3.grid_disk(h3.latlng_to_cell(40.7033, -74.0170, 8), 2))
    for row in _attach([INLAND_CELL, *coastal]):
        geom = shape(
            {"type": row["boundary_type"], "coordinates": row["boundary"]}
        )
        assert not geom.is_empty
        assert geom.is_valid


def _mean_vertex_radius_m(ring: list[list[float]]) -> float:
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    clng = sum(p[0] for p in pts) / len(pts)
    clat = sum(p[1] for p in pts) / len(pts)
    total = 0.0
    for lng, lat in pts:
        dx = (lng - clng) * 111320.0 * math.cos(math.radians(clat))
        dy = (lat - clat) * 111320.0
        total += math.hypot(dx, dy)
    return total / len(pts)


# --------------------------------------------------------------------------- #
# The cell is the real cell
# --------------------------------------------------------------------------- #


def test_inland_cell_is_the_untouched_hexagon():
    """The regression that made the map look perforated.

    hexApprox(lat, lng, 0.0025) gave a mean vertex radius of ~278 m where the
    true r8 cell is ~535 m, a ratio of 0.52. Anything below 0.98 means the ring
    is not the cell. Clipping must also leave an inland cell alone.
    """
    row = _attach([INLAND_CELL])[0]
    ring = _rings(row)[0]
    assert len(ring) == 7, "an untouched H3 cell has six vertices plus the closing one"
    assert ring[0] == ring[-1], "GeoJSON polygons must close"

    true_ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(INLAND_CELL)]
    ratio = _mean_vertex_radius_m(ring) / _mean_vertex_radius_m(true_ring)
    assert 0.98 < ratio < 1.02, f"cell drawn at {ratio:.3f} of its true size"


def test_ring_is_lnglat_order():
    """NYC sits near -74 lng / +40 lat, so the two are unmistakable."""
    for row in _attach([INLAND_CELL]):
        for ring in _rings(row):
            for lng, lat in ring:
                assert -75 < lng < -73, f"first coordinate should be longitude, got {lng}"
                assert 40 < lat < 41, f"second coordinate should be latitude, got {lat}"


def test_adjacent_inland_cells_share_their_edge_vertices():
    """Contiguity has to survive the rounding applied to the coordinates.

    Six decimals is ~0.1 m, far below a pixel at any zoom, but if it were
    loosened neighbouring rings would stop meeting and the seams would reappear.
    """
    neighbours = [c for c in h3.grid_disk(INLAND_CELL, 1) if c != INLAND_CELL]
    rows = _attach([INLAND_CELL, neighbours[0]])
    assert len(rows) == 2, "both inland cells should survive clipping"
    a = {tuple(p) for p in _rings(rows[0])[0]}
    b = {tuple(p) for p in _rings(rows[1])[0]}
    assert len(a & b) >= 2, "neighbouring cells must share the two vertices of their edge"


# --------------------------------------------------------------------------- #
# The cell stops at the water
# --------------------------------------------------------------------------- #


def _land_mask_available() -> bool:
    return DirectQueryDataProvider._land_mask() is not None


requires_land = pytest.mark.skipif(
    not _land_mask_available(),
    reason="nta_2020.geojson unavailable; cells are served unclipped",
)


@requires_land
def test_open_water_cell_is_dropped_entirely():
    """Not dimmed, not greyed -- removed.

    Keeping a water cell with a muted colour would still assert a score for a
    patch of river; the honest move is to make no claim there at all.
    """
    assert _attach([OPEN_WATER_CELL]) == []


@requires_land
def test_clipped_cells_stay_on_land():
    """No part of any served cell may cover water."""
    import geopandas as gpd
    from shapely.geometry import shape

    nta = gpd.read_file(
        "/mnt/data/Urban-Dossier/data/boundaries/nta_2020.geojson"
    )
    # The same simplification the provider clips against, plus a hair of
    # tolerance for the six-decimal rounding it applies afterwards.
    land = nta.geometry.union_all().simplify(0.0001, preserve_topology=True)
    tolerant = land.buffer(0.00002)

    coastal = list(h3.grid_disk(h3.latlng_to_cell(40.7033, -74.0170, 8), 2))
    rows = _attach(coastal)
    assert rows, "expected some Lower Manhattan cells to survive"

    for row in rows:
        geom = shape({"type": row.get("boundary_type", "Polygon"),
                      "coordinates": row["boundary"]})
        spill = geom.difference(tolerant).area / geom.area
        assert spill < 0.01, (
            f"{row['h3']} puts {spill * 100:.1f}% of its area over water"
        )


@requires_land
def test_split_cells_keep_every_part():
    """A cell cut by a channel must not silently lose one side.

    Clipping such a cell yields a MultiPolygon; emitting it as a Polygon would
    quietly drop all but the first piece.
    """
    around = list(h3.grid_disk(h3.latlng_to_cell(40.7033, -74.0170, 8), 3))
    rows = _attach(around)
    multi = [r for r in rows if r.get("boundary_type") == "MultiPolygon"]
    assert multi, "expected at least one cell split by water near the harbour"
    for row in multi:
        assert len(row["boundary"]) >= 2, "a MultiPolygon needs more than one part"
        for poly in row["boundary"]:
            assert poly and len(poly[0]) >= 4, "each part needs a closed ring"


@requires_land
def test_land_fraction_is_reported_for_clipped_cells():
    coastal = list(h3.grid_disk(h3.latlng_to_cell(40.7033, -74.0170, 8), 2))
    clipped = [r for r in _attach(coastal) if r.get("land_fraction") is not None]
    assert clipped, "coastal cells should report how much of them is land"
    for row in clipped:
        assert 0 < row["land_fraction"] <= 1


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #


def test_unparseable_index_is_skipped_not_fatal():
    rows = DirectQueryDataProvider._attach_cell_boundaries(
        [{"h3": "not-an-h3-index"}, {"h3": INLAND_CELL}]
    )
    assert len(rows) == 2, "one bad row must not drop the rest of the overview"
    bad = next(r for r in rows if r["h3"] == "not-an-h3-index")
    assert "boundary" not in bad, "a bad index should be skipped, not guessed at"
