"""The metric registry has to be reachable, not just correct.

The acceptance criterion for the registry is that any score shown in the UI can
be traced back to its definition, unit, direction and methodology version. That
is an API property, so it gets API tests: a registry that is perfect in Python
and unreachable over HTTP has not met it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from urban_dossier_backend.app import app
from urban_dossier_backend.metrics import METHODOLOGY_VERSION, METRICS_BY_ID


client = TestClient(app)


def test_registry_endpoint_lists_every_metric():
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    payload = resp.json()
    assert {m["id"] for m in payload["metrics"]} == set(METRICS_BY_ID)
    assert payload["methodology_version"] == METHODOLOGY_VERSION


def test_every_metric_is_individually_addressable():
    """One request per metric id -- the lookup a UI tooltip would make."""
    for metric_id in METRICS_BY_ID:
        resp = client.get(f"/api/metrics/{metric_id}")
        assert resp.status_code == 200, metric_id
        body = resp.json()
        assert body["id"] == metric_id


def test_a_metric_response_answers_the_four_acceptance_questions():
    """Definition, unit, direction, methodology version -- all four, populated."""
    body = client.get("/api/metrics/collision").json()
    assert body["description"].strip()
    assert body["unit"] == "collisions within 500 m"
    assert body["direction"] == "lower_is_better"
    assert body["methodology_version"] == METHODOLOGY_VERSION


def test_grain_is_disclosed_for_a_zip_level_metric():
    """A ZIP metric drawn on hexagons must still report itself as ZIP."""
    body = client.get("/api/metrics/ems_response").json()
    assert body["spatial_grain"] == "zip"


def test_the_removed_collision_copy_is_a_404_not_a_ghost():
    """collision_transport left the registry in v3.8.0; the API must agree."""
    resp = client.get("/api/metrics/collision_transport")
    assert resp.status_code == 404
    assert "collision" in resp.json()["known"]


def test_unknown_metric_is_a_404_that_says_what_exists():
    resp = client.get("/api/metrics/does_not_exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "does_not_exist" in body["detail"]
    assert "collision" in body["known"]


def test_unknown_metric_does_not_fall_through_to_the_registry_route():
    """Guards the route ordering: /api/metrics/{id} must not shadow /api/metrics."""
    listing = client.get("/api/metrics").json()
    assert "metrics" in listing
    assert "id" not in listing
