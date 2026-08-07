"""Overview cells must carry their true H3 boundary.

The map draws the choropleth from these rings. When the backend supplied only a
centre point, the Node proxy invented a hexagon around it with a hardcoded
0.0025-degree radius -- against an r8 cell's ~0.0048 degrees, that painted 27%
of each cell's area and left the remaining 73% as bare basemap. The grid tiles
the city with no holes, so what looked like missing data was entirely a
geometry bug.

These tests pin the ring to the real cell so the gap cannot come back, and so
nobody re-derives geometry in the proxy layer instead.
"""

from __future__ import annotations

import math

import h3
import pytest

from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider


def _mean_vertex_radius_m(ring_lnglat: list[list[float]]) -> float:
    """Mean distance from centroid to vertex, in metres."""
    pts = ring_lnglat[:-1]  # drop the closing vertex
    clng = sum(p[0] for p in pts) / len(pts)
    clat = sum(p[1] for p in pts) / len(pts)
    total = 0.0
    for lng, lat in pts:
        dx = (lng - clng) * 111320.0 * math.cos(math.radians(clat))
        dy = (lat - clat) * 111320.0
        total += math.hypot(dx, dy)
    return total / len(pts)


@pytest.fixture
def rows_with_boundary():
    cells = ["882a100003fffff", "882a1072d5fffff", "882a107289fffff"]
    return DirectQueryDataProvider._attach_cell_boundaries(
        [{"h3": c, "overall_score": 50} for c in cells]
    )


def test_every_cell_gets_a_closed_ring(rows_with_boundary):
    for row in rows_with_boundary:
        ring = row["boundary"]
        assert len(ring) == 7, "an H3 cell has six vertices plus the closing one"
        assert ring[0] == ring[-1], "GeoJSON polygons must close"


def test_ring_is_lnglat_order(rows_with_boundary):
    """NYC sits near -74 lng / +40 lat, so the two are unmistakable."""
    for row in rows_with_boundary:
        for lng, lat in row["boundary"]:
            assert -75 < lng < -73, f"first coordinate should be longitude, got {lng}"
            assert 40 < lat < 41, f"second coordinate should be latitude, got {lat}"


def test_ring_matches_the_real_cell_not_a_shrunken_approximation(rows_with_boundary):
    """The regression that made the map look perforated.

    hexApprox(lat, lng, 0.0025) produced a mean vertex radius of ~278 m where
    the true r8 cell is ~535 m -- a ratio of 0.52. Anything below 0.98 here
    means the ring is not the cell.
    """
    for row in rows_with_boundary:
        served = _mean_vertex_radius_m(row["boundary"])
        true_ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(row["h3"])]
        true_ring.append(true_ring[0])
        expected = _mean_vertex_radius_m(true_ring)
        ratio = served / expected
        assert 0.98 < ratio < 1.02, (
            f"{row['h3']} drawn at {ratio:.3f} of its true size "
            f"({served:.0f} m vs {expected:.0f} m)"
        )


def test_adjacent_cells_share_their_edge_vertices(rows_with_boundary):
    """Contiguity has to survive the rounding applied to the coordinates.

    Rounding to six decimals is ~0.1 m, far below a pixel at any zoom, but if
    it were ever loosened neighbouring rings would stop meeting and the seams
    would reappear.
    """
    cell = rows_with_boundary[0]["h3"]
    neighbour = next(c for c in h3.grid_disk(cell, 1) if c != cell)
    rings = DirectQueryDataProvider._attach_cell_boundaries(
        [{"h3": cell}, {"h3": neighbour}]
    )
    a = {(round(x, 6), round(y, 6)) for x, y in rings[0]["boundary"]}
    b = {(round(x, 6), round(y, 6)) for x, y in rings[1]["boundary"]}
    assert len(a & b) >= 2, "neighbouring cells must share the two vertices of their edge"


def test_unparseable_index_is_skipped_not_fatal():
    rows = DirectQueryDataProvider._attach_cell_boundaries(
        [{"h3": "not-an-h3-index"}, {"h3": "882a100003fffff"}]
    )
    assert "boundary" not in rows[0], "a bad index should be skipped"
    assert "boundary" in rows[1], "one bad row must not drop the rest of the overview"
