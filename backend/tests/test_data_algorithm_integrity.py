from __future__ import annotations

import importlib.util
import math
import subprocess
import sys
from pathlib import Path

import pytest

from urban_dossier_backend import config
from urban_dossier_backend.hotspot_engine import detect_hotspots
from urban_dossier_backend.secondary_scoring import compute_scores_with_coverage
from urban_dossier_backend.trend_engine import (
    compute_recent_delta,
    compute_seasonal_delta,
    compute_anomaly,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ready_validator_tracks_registered_context_and_environment_publications():
    validator = _load_script("validate_ready_parquet")
    assert {
        "context/population_r9.parquet",
        "context/population_r9_provenance.parquet",
        "environment/hvi_scores_zip.parquet",
        "environment/nyccas_no_scores_h3.parquet",
    } <= validator.EXPECTED_FILES


def test_raw_audit_is_a_gate_and_models_street_centerline_as_auxiliary(tmp_path, monkeypatch):
    audit_module = _load_script("audit_datasets")
    monkeypatch.setattr(
        audit_module,
        "DATASETS",
        (audit_module.DatasetExpectation("safety/required.csv", ("id",)),),
    )
    (tmp_path / "safety").mkdir()
    (tmp_path / "transit").mkdir()
    (tmp_path / "safety" / "required.csv").write_text("id\n1\n", encoding="utf-8")
    (tmp_path / "transit" / "nyc_street_centerline.csv").write_text(
        "id\n1\n", encoding="utf-8"
    )

    report = audit_module.audit(tmp_path)

    assert report["status"] == "ok"
    assert report["auxiliary_csv_files"] == ["transit/nyc_street_centerline.csv"]
    assert report["unexpected_csv_files"] == []

    (tmp_path / "transit" / "surprise.csv").write_text("id\n1\n", encoding="utf-8")
    report = audit_module.audit(tmp_path)
    assert report["status"] == "invalid"
    assert report["unexpected_csv_files"] == ["transit/surprise.csv"]


def test_raw_audit_cli_exits_nonzero_for_an_incomplete_snapshot(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_datasets.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1


def test_data_paths_are_stable_path_contracts():
    assert isinstance(config.DATA_ROOT, Path)
    assert isinstance(config.CACHE_DIR, Path)
    assert isinstance(config.BOUNDARIES_DIR, Path)


def test_missing_trend_windows_do_not_become_zero_counts():
    assert compute_recent_delta({"last_30d": None, "prev_30d": 10}) == {
        "change_pct": None,
        "label": "no_baseline",
    }
    assert compute_seasonal_delta({"last_90d": float("nan"), "same_90d_last_year": 10}) == {
        "change_pct": None,
        "label": "no_baseline",
    }


def test_non_finite_quarters_are_excluded_from_anomaly_statistics():
    result = compute_anomaly({
        "quarterly_series": [
            {"period": "2025-Q1", "value": 1},
            {"period": "2025-Q2", "value": 2},
            {"period": "2025-Q3", "value": math.nan},
            {"period": "2025-Q4", "value": 3},
            {"period": "2026-Q1", "value": 20},
        ]
    })
    assert result["n_observations"] == 4
    assert result["latest_period"] == "2026-Q1"


def test_live_provider_collision_shape_still_scores_safety_without_ready_tables():
    state = {
        "safety": {},
        "transit": {"collision_count_500m": 20, "ped_cyclist_injuries_1km": 2},
        "amenities": {},
        "building": {},
    }
    scores, coverage = compute_scores_with_coverage(
        state, {"collision": {"p75": 40}}
    )
    assert scores["safety"] == 71
    assert coverage["safety"]["present"] == ["collision"]


def test_hotspots_use_meter_distance_and_ignore_invalid_coordinates(monkeypatch):
    monkeypatch.setattr(
        "urban_dossier_backend.hotspot_engine._cluster_gpu", lambda *_args: None
    )
    latitude = 40.7
    ninety_metres_lon = 90 / (111_195 * math.cos(math.radians(latitude)))
    incidents = [
        {"latitude": latitude, "longitude": -74.0, "kind": "collision"},
        {
            "latitude": latitude,
            "longitude": -74.0 + ninety_metres_lon,
            "kind": "collision",
        },
        {"latitude": float("nan"), "longitude": -74.0, "kind": "bad"},
    ]

    hotspots = detect_hotspots(incidents, eps_meters=100, min_samples=2)

    assert len(hotspots) == 1
    assert hotspots[0]["incident_count"] == 2
    assert hotspots[0]["types"] == {"collision": 2}
    assert hotspots[0]["radius_m"] == pytest.approx(45, abs=1)
