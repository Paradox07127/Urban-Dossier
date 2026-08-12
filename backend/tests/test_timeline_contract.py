from __future__ import annotations

from fastapi.testclient import TestClient

from urban_dossier_backend.app import app
from urban_dossier_backend.metrics import METHODOLOGY_VERSION
from urban_dossier_backend.periods import current_quarter
from urban_dossier_backend.timeline import TIMELINE_COLORS, timeline_geojson


def test_real_timeline_is_period_keyed_and_server_classified():
    payload = timeline_geojson("collision", 8)
    metadata = payload["metadata"]

    assert metadata["available"] is True
    assert metadata["methodology_version"] == METHODOLOGY_VERSION
    assert metadata["cell_count"] == len(payload["features"]) > 0
    assert len(metadata["periods"]) == 8
    assert metadata["default_period"] == metadata["periods"][-1]["period"]
    assert metadata["animation"] == {
        "state_property": "timeline_period",
        "lookup": "period-keyed MapLibre match expression",
        "tick_mutation": "setGlobalStateProperty",
    }
    assert all("-Q" in period["period"] for period in metadata["periods"])
    assert all(period["colors"][0] == TIMELINE_COLORS[0] for period in metadata["periods"])

    first = payload["features"][0]
    for period in metadata["periods"]:
        value = first["properties"][period["value_property"]]
        color = first["properties"][period["color_property"]]
        class_index = sum(value >= edge for edge in period["breaks"])
        assert color == period["colors"][class_index]


def test_period_completeness_matches_the_current_calendar_quarter():
    payload = timeline_geojson("311_sanitation", 8)
    latest = payload["metadata"]["periods"][-1]

    assert latest["period"] == payload["metadata"]["default_period"]
    assert latest["period_complete"] is (latest["period"] != current_quarter())


def test_timeline_http_contract_and_fail_closed_validation():
    client = TestClient(app)
    response = client.get("/api/timeline?signal=collision&limit_periods=4")
    assert response.status_code == 200
    assert len(response.json()["metadata"]["periods"]) == 4

    invalid = client.get("/api/timeline?signal=imaginary")
    assert invalid.status_code == 422
    assert "imaginary" not in invalid.json()["detail"]

    invalid_limit = client.get("/api/timeline?limit_periods=1000")
    assert invalid_limit.status_code == 422
