"""Tests for the isochrone and simulation scenario layer.

Pure logic only: no OSM graph, no Parquet, no network. The parts that need
built artifacts are covered by their absence-handling paths, which matter
because a fresh deployment has neither artifact until its build step runs.
"""

from __future__ import annotations

import json

from urban_dossier_backend.scenarios import _curve_lookup, simulate_intervention


# --------------------------------------------------------------------------- #
# Curve lookup
# --------------------------------------------------------------------------- #

CURVE = [[1.0, 10.0], [2.0, 30.0], [5.0, 60.0], [10.0, 90.0]]


def test_below_observed_range_clamps_to_the_first_point():
    assert _curve_lookup(CURVE, 2.0, 0.0) == 10.0
    assert _curve_lookup(CURVE, 2.0, 1.0) == 10.0


def test_exact_knots_return_their_own_value():
    assert _curve_lookup(CURVE, 2.0, 2.0) == 30.0
    assert _curve_lookup(CURVE, 2.0, 10.0) == 90.0


def test_between_knots_interpolates_linearly():
    # Midway between (2,30) and (5,60) is 3.5 -> 45.
    assert _curve_lookup(CURVE, 2.0, 3.5) == 45.0


def test_extrapolation_is_capped_at_100():
    """A score is a 0-100 index; the fitted slope must not push it past that."""

    assert _curve_lookup(CURVE, 50.0, 100.0) == 100.0


def test_extrapolation_never_falls_below_the_last_observed_score():
    """Scoring is monotone in count -- more assets must not score worse."""

    assert _curve_lookup(CURVE, -10.0, 50.0) == 90.0


def test_empty_curve_is_not_a_crash():
    assert _curve_lookup([], 1.0, 5.0) != _curve_lookup([], 1.0, 5.0)  # nan != nan


# --------------------------------------------------------------------------- #
# Missing-artifact handling
# --------------------------------------------------------------------------- #


def test_missing_elasticity_artifact_returns_a_structured_error(tmp_path, monkeypatch):
    monkeypatch.setenv("URBAN_DOSSIER_ELASTICITY_PATH", str(tmp_path / "nope.json"))

    result = simulate_intervention(40.7265, -73.9815, "toilet", count=1)

    assert "error" in result
    # The agent needs to know how to fix it, not just that it broke.
    assert "fit_intervention_elasticity" in result["retry_hint"]


def test_unknown_intervention_lists_the_valid_ones(tmp_path, monkeypatch):
    artifact = {
        "method": "empirical_conditional_mean",
        "interventions": {
            "toilet": {"available": True, "curve": [[1, 10]]},
            "park": {"available": True, "curve": [[1, 10]]},
        },
    }
    path = tmp_path / "elasticity.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setenv("URBAN_DOSSIER_ELASTICITY_PATH", str(path))

    result = simulate_intervention(40.7265, -73.9815, "monorail", count=1)

    assert "error" in result
    assert result["available"] == ["park", "toilet"]


def test_unavailable_intervention_surfaces_the_fit_reason(tmp_path, monkeypatch):
    artifact = {
        "interventions": {
            "bus_stop": {"available": False, "reason": "only 3 usable rows"}
        }
    }
    path = tmp_path / "elasticity.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setenv("URBAN_DOSSIER_ELASTICITY_PATH", str(path))

    result = simulate_intervention(40.7265, -73.9815, "bus_stop", count=1)

    assert "only 3 usable rows" in result["error"]
