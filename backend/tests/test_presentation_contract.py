from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from urban_dossier_backend.app import app
from urban_dossier_backend.metrics import METHODOLOGY_VERSION
from urban_dossier_backend.presentation import (
    BIVARIATE_COLORS,
    SCORE_COLORS,
    palette_cvd_report,
    bivariate_geojson,
    presentation_contract,
    quantile_breaks,
)


def test_quantile_breaks_are_server_owned_and_do_not_invent_tied_classes():
    assert quantile_breaks(list(range(1, 101)), 5) == [20.8, 40.6, 60.4, 80.2]
    assert quantile_breaks([50] * 20, 5) == [50.0]


def test_published_palettes_pass_adjacent_cvd_gate():
    univariate = palette_cvd_report(
        SCORE_COLORS,
        [(index, index + 1) for index in range(len(SCORE_COLORS) - 1)],
    )
    flat = [color for row in BIVARIATE_COLORS for color in row]
    edges = [
        *((row * 3 + column, row * 3 + column + 1) for row in range(3) for column in range(2)),
        *((row * 3 + column, (row + 1) * 3 + column) for row in range(2) for column in range(3)),
    ]
    bivariate = palette_cvd_report(flat, edges)

    assert univariate["passes"] is True
    assert bivariate["passes"] is True
    assert min(univariate["minimum_adjacent_delta_e"].values()) >= 8
    assert min(bivariate["minimum_adjacent_delta_e"].values()) >= 8


def test_real_contract_uses_land_clipped_overview_population():
    contract = presentation_contract("safety", "amenities")

    assert contract["methodology_version"] == METHODOLOGY_VERSION
    assert contract["univariate"]["accessibility"]["passes"] is True
    for category, spec in contract["univariate"]["categories"].items():
        assert spec["category"] == category
        assert spec["population"] == "land_clipped_h3_r8_cells"
        assert spec["population_n"] > 0
        assert spec["breaks"] == sorted(set(spec["breaks"]))
        assert len(spec["colors"]) == len(spec["breaks"]) + 1
    assert contract["bivariate"]["x"]["category"] == "safety"
    assert contract["bivariate"]["y"]["category"] == "amenities"
    assert len(contract["bivariate"]["matrix"]) == 3
    assert all(len(row) == 3 for row in contract["bivariate"]["matrix"])


def test_unknown_bivariate_category_fails_closed():
    with pytest.raises(ValueError, match="categories must be"):
        presentation_contract("safety", "imaginary")


def test_presentation_contract_is_addressable_over_http():
    response = TestClient(app).get(
        "/api/presentation/classes?x_category=transit&y_category=amenities"
    )
    assert response.status_code == 200
    assert response.json()["bivariate"]["x"]["category"] == "transit"


def test_presentation_endpoint_rejects_unknown_categories():
    response = TestClient(app).get(
        "/api/presentation/classes?x_category=transit&y_category=imaginary"
    )
    assert response.status_code == 422
    assert "imaginary" not in response.json()["detail"]  # do not echo arbitrary input


def test_bivariate_geojson_joins_two_real_h3_populations():
    payload = bivariate_geojson("safety", "transit")
    assert payload["metadata"]["cell_count"] == len(payload["features"]) > 0
    assert payload["metadata"]["methodology_version"] == METHODOLOGY_VERSION
    for feature in payload["features"][:20]:
        props = feature["properties"]
        assert 0 <= props["x_class"] <= 2
        assert 0 <= props["y_class"] <= 2
        assert props["bivariate_color"] == BIVARIATE_COLORS[props["y_class"]][
            props["x_class"]
        ]
        assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_bivariate_geojson_endpoint_is_addressable():
    response = TestClient(app).get(
        "/api/presentation/bivariate?x_category=safety&y_category=transit"
    )
    assert response.status_code == 200
    assert response.json()["metadata"]["cell_count"] > 0
