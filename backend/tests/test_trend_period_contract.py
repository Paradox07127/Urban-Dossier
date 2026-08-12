from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

import importlib.util as _ilu
from pathlib import Path as _P

# scripts/ is not a package; load the module the way every other test here
# does, so the suite collects under the established `cd backend` convention.
_spec = _ilu.spec_from_file_location(
    "preprocess_common", _P(__file__).resolve().parents[1] / "scripts" / "preprocess_common.py"
)
_pc = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_pc)
quarter_label = _pc.quarter_label
from urban_dossier_backend.pattern_detector import (
    _aligned_pair,
    _assemble_pattern,
    _layer1_correlations,
)
from urban_dossier_backend.periods import canonical_quarter
from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider
from urban_dossier_backend.trend_engine import (
    compute_all_trends,
    compute_anomaly,
    compute_persistence,
)
from urban_dossier_backend.utils import is_within_days


def _points(*values: tuple[str, int | None, float | None]) -> dict:
    return {
        "quarterly_series": [
            {"period": period, "value": value, "coverage": coverage}
            for period, value, coverage in values
        ]
    }


def test_canonical_quarter_normalizes_artifact_keys_and_rejects_bad_dates():
    as_of = date(2026, 8, 12)

    assert canonical_quarter("2025Q4", as_of=as_of) == "2025-Q4"
    assert canonical_quarter("2026-Q3", as_of=as_of) == "2026-Q3"
    assert canonical_quarter("1900Q1", as_of=as_of) is None
    assert canonical_quarter("219Q3", as_of=as_of) is None
    assert canonical_quarter("2026Q4", as_of=as_of) is None


def test_ready_provider_preserves_period_and_spatial_coverage(tmp_path, monkeypatch):
    artifact = tmp_path / "quarterly.parquet"
    artifact.touch()
    provider = DirectQueryDataProvider()
    monkeypatch.setattr(provider, "_ready_path", lambda _relative: artifact)
    monkeypatch.setattr(
        provider,
        "_query_rows",
        lambda *_args: [
            {"quarter": "1900Q1", "count": 99, "coverage_n": 2},
            {"quarter": "2026Q1", "count": 5, "coverage_n": 1},
            {"quarter": "2025Q4", "count": 3, "coverage_n": 2},
        ],
    )

    result = provider._query_ready_quarterly(object(), "unused.parquet", ["a", "b"])

    assert result is not None
    assert result["quarterly_series"] == [
        {
            "period": "2025-Q4",
            "value": 3,
            "coverage": 1.0,
            "coverage_n": 2,
            "coverage_total": 2,
            "period_complete": True,
        },
        {
            "period": "2026-Q1",
            "value": 5,
            "coverage": 0.5,
            "coverage_n": 1,
            "coverage_total": 2,
            "period_complete": True,
        },
    ]
    assert "quarterly_values" not in result


def test_trend_engine_never_relabels_periods_from_today():
    historical = {
        "collision": _points(
            ("2024-Q4", 2, 0.5),
            ("2025-Q1", None, 0.0),
            ("2025-Q2", 4, 0.75),
            ("2026-Q1", 8, 1.0),
        )
    }

    trend = compute_all_trends(
        {"transit": {"collision_count_500m": 8}}, historical, {}
    )["collision"]

    assert trend["quarterly_series"] == [
        {"period": "2024-Q4", "value": 2, "coverage": 0.5, "period_complete": True},
        {"period": "2025-Q1", "value": None, "coverage": 0.0, "period_complete": True},
        {"period": "2025-Q2", "value": 4, "coverage": 0.75, "period_complete": True},
        {"period": "2026-Q1", "value": 8, "coverage": 1.0, "period_complete": True},
    ]
    assert trend["quarterly_methodology"]["alignment"] == "calendar key"


def test_persistence_breaks_on_a_missing_calendar_quarter():
    result = compute_persistence(
        _points(("2025-Q4", 20, 1.0), ("2026-Q2", 30, 1.0)),
        threshold=10,
    )

    assert result["consecutive_above"] == 1
    assert result["n_observations"] == 2
    assert "missing calendar quarter" in result["missing_data_policy"]


def test_anomaly_discloses_method_and_minimum_sample():
    result = compute_anomaly(
        _points(
            ("2025-Q2", 1, 1.0),
            ("2025-Q3", 2, 1.0),
            ("2025-Q4", 3, 1.0),
            ("2026-Q1", 20, 1.0),
        )
    )

    assert result["latest_period"] == "2026-Q1"
    assert result["n_observations"] == result["minimum_observations"] == 4
    assert result["method"].startswith("z_score")
    assert "partial current quarter excluded" in result["missing_data_policy"]


def test_partial_current_quarter_is_visible_but_excluded_from_statistics():
    values = _points(
        ("2025-Q2", 10, 1.0),
        ("2025-Q3", 10, 1.0),
        ("2025-Q4", 10, 1.0),
        ("2026-Q1", 10, 1.0),
        ("2026-Q2", 10, 1.0),
        ("2026-Q3", 1000, 0.5),
    )
    values["quarterly_series"][-1]["period_complete"] = False

    trend = compute_all_trends(
        {"transit": {"collision_count_500m": 1000}}, {"collision": values}, {}
    )["collision"]

    assert trend["quarterly_series"][-1]["period"] == "2026-Q3"
    assert trend["quarterly_series"][-1]["period_complete"] is False
    assert trend["anomaly"]["latest_period"] == "2026-Q2"
    assert trend["anomaly"]["is_anomaly"] is False


def test_pattern_correlation_inner_joins_real_periods():
    left = {"2025-Q1": 1, "2025-Q2": 2, "2025-Q3": 3, "2025-Q4": 4, "2026-Q1": 5}
    right = {
        "2024-Q4": 100,
        "2025-Q1": 2,
        "2025-Q2": 4,
        "2025-Q3": 6,
        "2025-Q4": 8,
        "2026-Q1": 10,
    }

    a, b, periods = _aligned_pair(left, right)
    assert periods == ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1"]
    assert a == [1, 2, 3, 4, 5]
    assert b == [2, 4, 6, 8, 10]

    candidates = _layer1_correlations({"collision": left, "rodent": right})
    assert len(candidates) == 1
    candidate = candidates[0]
    candidate.update({"direction_a": "worsening", "direction_b": "worsening"})
    pattern = _assemble_pattern(candidate, None)
    assert pattern is not None
    assert pattern["period_alignment"] == "period_key_inner_join"
    assert pattern["n_observations"] == 5
    assert pattern["period_start"] == "2025-Q1"
    assert pattern["period_end"] == "2026-Q1"


def test_future_rows_are_not_treated_as_recent():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert is_within_days(tomorrow, 30) is False


def test_preprocessor_quarantines_placeholder_and_future_quarters():
    today = date.today().isoformat()
    labels = quarter_label(pd.Series(["1900-01-01", "0219-07-01", today, "2099-01-01"]))

    assert pd.isna(labels.iloc[0])
    assert pd.isna(labels.iloc[1])
    assert labels.iloc[2] == f"{date.today().year}Q{((date.today().month - 1) // 3) + 1}"
    assert pd.isna(labels.iloc[3])
