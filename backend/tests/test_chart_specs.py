from __future__ import annotations

import json

import urban_dossier_backend.service as service
from urban_dossier_backend.chart_specs import (
    METHODOLOGY_VERSION,
    compare_scores_chart,
    detail_chart_specs,
    score_composition_chart,
    score_distribution_chart,
    trend_chart,
)
from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider


def _values(chart) -> list[dict]:
    return chart.spec["data"]["values"]


def test_score_chart_carries_code_provenance_and_exact_backend_values():
    chart = score_composition_chart(
        {"overall": 61, "safety": 55.25, "transit": None, "amenities": 72},
        {
            "overall": {"effective_ratio": 0.75},
            "safety": {"ratio": 0.5},
        },
    )

    assert chart.code_ref.endswith("score_composition_chart@1")
    assert chart.methodology_version == METHODOLOGY_VERSION
    assert chart.spec["usermeta"]["code_ref"] == chart.code_ref
    assert _values(chart) == [
        {"category": "overall", "label": "Overall", "score": 61.0, "coverage": 0.75},
        {"category": "safety", "label": "Safety", "score": 55.25, "coverage": 0.5},
        {"category": "amenities", "label": "Amenities", "score": 72.0, "coverage": 1.0},
    ]
    assert "transit" not in {row["category"] for row in _values(chart)}
    json.dumps(chart.model_dump())


def test_trend_chart_uses_real_period_keys_and_omits_missing_values():
    chart = trend_chart(
        {
            "collision": {
                "quarterly_series": [
                    {"quarter": "2025-Q4", "count": 3},
                    {"quarter": "2026-Q1", "count": None},
                    {"quarter": "2026-Q2", "count": 5},
                ]
            },
            "rodent": {"quarterly_series": [{"quarter": "2026-Q2", "count": 2}]},
        }
    )

    assert chart is not None
    assert [row["quarter"] for row in _values(chart)] == ["2025-Q4", "2026-Q2", "2026-Q2"]
    assert chart.spec["encoding"]["x"]["sort"] == ["2025-Q4", "2026-Q1", "2026-Q2"]
    assert "transform" not in chart.spec


def test_compare_chart_uses_supplied_backend_delta_without_recalculation():
    chart = compare_scores_chart(
        {"overall": 20, "safety": 40},
        {"overall": 90, "safety": 41},
        {"overall": -7.5, "safety": 11.25},
    )

    rows = _values(chart)
    assert {row["delta_b_minus_a"] for row in rows if row["category"] == "overall"} == {
        -7.5
    }
    assert {row["delta_b_minus_a"] for row in rows if row["category"] == "safety"} == {
        11.25
    }
    serialized = json.dumps(chart.model_dump())
    assert '"calculate"' not in serialized
    assert '"transform"' not in serialized


def test_detail_bundle_has_no_empty_trend_placeholder():
    charts = detail_chart_specs(
        {"overall": 50},
        {"overall": {"ratio": 1}},
        {"collision": {"quarterly_series": []}},
    )

    assert set(charts) == {"score_composition"}


def test_overview_distribution_bins_and_midrank_share_one_population():
    distribution = DirectQueryDataProvider._overview_score_distribution(
        [
            {"overall_score": 0},
            {"overall_score": 4},
            {"overall_score": 5},
            {"overall_score": 50},
            {"overall_score": 100},
            {"overall_score": None},
        ],
        "overall_score",
        5,
    )

    assert distribution is not None
    assert distribution["grain"] == "h3_r8_land_cells"
    assert distribution["population_n"] == 5
    assert sum(item["count"] for item in distribution["bins"]) == 5
    assert distribution["bins"][0]["count"] == 2
    assert distribution["bins"][1]["count"] == 1
    assert distribution["bins"][10]["count"] == 1
    assert distribution["bins"][19]["count"] == 1
    assert distribution["marker_percentile"] == 0.5
    assert DirectQueryDataProvider._overview_score_distribution([], "overall_score", 5) is None


def test_distribution_chart_layers_cell_marker_and_sensitivity_interval():
    overview_context = {
        "overall": {
            "distribution": {
                "bins": [
                    {"bin_start": 0, "bin_end": 5, "count": 3},
                    {"bin_start": 5, "bin_end": 10, "count": 7},
                ],
                "marker_score": 54,
                "marker_percentile": 0.72,
            }
        }
    }
    chart = score_distribution_chart(
        overview_context,
        {
            "score_range": [48, 61],
            "distribution": {
                "grain": "h3_r9_analysis_cells",
                "bins": [
                    {"bin_start": 0, "bin_end": 5, "count": 11},
                    {"bin_start": 5, "bin_end": 10, "count": 13},
                ],
                "marker_score": 52,
                "marker_percentile": 0.68,
            },
        },
    )

    assert chart is not None
    assert chart.code_ref.endswith("score_distribution_chart@1")
    assert len(chart.spec["layer"]) == 3
    assert chart.spec["layer"][0]["data"]["values"] == [
        {"range_start": 48.0, "range_end": 61.0}
    ]
    assert chart.spec["layer"][1]["data"]["values"] == [
        {"bin_start": 0, "bin_end": 5, "count": 11},
        {"bin_start": 5, "bin_end": 10, "count": 13},
    ]
    assert chart.spec["layer"][2]["data"]["values"] == [
        {"score": 52.0, "percentile": 0.68, "marker_label": "Center cell"}
    ]
    assert "transform" not in json.dumps(chart.model_dump())

    without_interval = score_distribution_chart(overview_context, None)
    assert without_interval is not None
    assert len(without_interval.spec["layer"]) == 2

    mismatched_interval = score_distribution_chart(
        overview_context,
        {"score_range": [48, 61]},
    )
    assert mismatched_interval is not None
    assert len(mismatched_interval.spec["layer"]) == 2


def test_preview_response_publishes_chart_specs(monkeypatch):
    class Provider:
        def get_point_signals(self, latitude, longitude, radius_m, _days):
            return {
                "target": {"latitude": latitude, "longitude": longitude, "radius_m": radius_m},
                "current_state": {"safety": {}, "transit": {}, "amenities": {}, "building": {}},
                "detail_items": {
                    "map_points": [],
                    "nearby_facilities": [],
                    "building_flags": [],
                    "recent_incidents": [],
                },
                "query_evidence": [],
            }

        def get_overview_context(self, *_args):
            return None

        def get_baselines(self):
            return {}

        def get_local_timeseries(self, *_args):
            return {}

    monkeypatch.setattr(service, "_provider_from_mode", lambda _mode=None: (Provider(), "test"))
    monkeypatch.setattr(
        service,
        "compute_scores_with_coverage",
        lambda *_args, **_kwargs: (
            {"overall": 64, "safety": 60, "transit": 70, "amenities": 62},
            {"overall": {"ratio": 0.75}},
        ),
    )
    monkeypatch.setattr(
        service,
        "compute_all_trends",
        lambda *_args: {
            "collision": {"quarterly_series": [{"quarter": "2026-Q2", "count": 4}]}
        },
    )
    monkeypatch.setattr(service, "detect_multi_signal_patterns", lambda *_args: [])
    monkeypatch.setattr(service, "compute_priority_actions", lambda **_kwargs: [])
    monkeypatch.setattr(service, "score_uncertainty", lambda *_args: None)

    payload = service.preview_point(
        latitude=40.75,
        longitude=-73.99,
        radius_m=500,
        priority_order=["safety", "transit", "amenities"],
        time_window_days=365,
    )

    assert set(payload["chart_specs"]) == {"score_composition", "recent_trends"}
    assert payload["chart_specs"]["score_composition"]["spec"]["data"]["values"][0][
        "score"
    ] == 64.0


def test_compare_response_publishes_backend_delta_chart(monkeypatch):
    payloads = iter(
        [
            {"scores": {"overall": 45, "safety": 50}},
            {"scores": {"overall": 60, "safety": 47}},
        ]
    )
    monkeypatch.setattr(service, "analyze_point", lambda **_kwargs: next(payloads))

    response = service.compare_points(
        point_a={"latitude": 40.7, "longitude": -73.9},
        point_b={"latitude": 40.8, "longitude": -74.0},
        radius_m=500,
        priority_order=["safety", "transit", "amenities"],
        time_window_days=365,
    )

    assert response["deltas"] == {"overall": 15.0, "safety": -3.0}
    values = response["chart_specs"]["compare_scores"]["spec"]["data"]["values"]
    assert {row["delta_b_minus_a"] for row in values if row["category"] == "overall"} == {
        15.0
    }
