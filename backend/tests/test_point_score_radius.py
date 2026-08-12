"""Prepared score aggregation must honour the radius shown in the UI."""

from __future__ import annotations

import h3
import pytest

from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider
from urban_dossier_backend.utils import haversine_m


LATITUDE = 40.7580
LONGITUDE = -73.9855


@pytest.mark.parametrize("radius_m", [200, 500, 1000])
def test_prepared_score_cells_have_centres_inside_requested_radius(radius_m: int):
    cells = DirectQueryDataProvider()._h3_cells_for_radius(
        LATITUDE, LONGITUDE, radius_m
    )

    assert cells
    assert all(
        haversine_m(LATITUDE, LONGITUDE, *h3.cell_to_latlng(cell)) <= radius_m
        for cell in cells
    )


def test_prepared_score_radius_is_monotonic_and_deterministic():
    provider = DirectQueryDataProvider()
    by_radius = {
        radius: provider._h3_cells_for_radius(LATITUDE, LONGITUDE, radius)
        for radius in (200, 500, 1000)
    }

    assert by_radius[200] == sorted(by_radius[200])
    assert set(by_radius[200]) < set(by_radius[500]) < set(by_radius[1000])
