"""Deterministic Vega-Lite contracts for public score visualizations.

The language model never writes these specs. Values arrive from the scoring
and trend engines, and this module only maps them into a fixed grammar. That
keeps every plotted number on the same auditable path as the JSON beside it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .metrics import METHODOLOGY_VERSION
from .presentation import score_color


VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
SCORE_ORDER = ["overall", "safety", "transit", "amenities", "building"]
TREND_ORDER = ["collision", "rodent", "housing_violations"]
TREND_LABELS = {
    "collision": "Collisions",
    "rodent": "Rodent positives",
    "housing_violations": "Housing violations",
}


class ChartSpec(BaseModel):
    """Versioned wrapper around one renderer-agnostic Vega-Lite document."""

    schema_version: Literal["1.0"] = "1.0"
    chart_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    code_ref: str = Field(min_length=1)
    methodology_version: str = METHODOLOGY_VERSION
    spec: dict[str, Any]


def _base_spec(code_ref: str, values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "$schema": VEGA_LITE_SCHEMA,
        "background": None,
        "autosize": {"type": "fit", "contains": "padding"},
        "data": {"values": values},
        "usermeta": {
            "code_ref": code_ref,
            "methodology_version": METHODOLOGY_VERSION,
        },
        "config": {
            "view": {"stroke": None},
            "axis": {
                "labelColor": "#667085",
                "titleColor": "#667085",
                "domainColor": "#d0d5dd",
                "gridColor": "#eaecf0",
                "labelFont": "IBM Plex Mono",
                "titleFont": "IBM Plex Mono",
            },
            "legend": {
                "labelColor": "#667085",
                "titleColor": "#667085",
                "labelFont": "IBM Plex Mono",
                "titleFont": "IBM Plex Mono",
            },
        },
    }


def score_composition_chart(
    scores: dict[str, Any], coverage: dict[str, Any] | None = None
) -> ChartSpec:
    code_ref = "urban_dossier_backend.chart_specs:score_composition_chart@1"
    coverage = coverage or {}
    values = []
    for category in SCORE_ORDER:
        score = scores.get(category)
        if not isinstance(score, (int, float)):
            continue
        ratio = (coverage.get(category) or {}).get("effective_ratio")
        if not isinstance(ratio, (int, float)):
            ratio = (coverage.get(category) or {}).get("ratio")
        values.append(
            {
                "category": category,
                "label": category.replace("_", " ").title(),
                "score": round(float(score), 2),
                "coverage": round(float(ratio), 4) if isinstance(ratio, (int, float)) else 1.0,
                "color": score_color(category, float(score)),
            }
        )

    spec = _base_spec(code_ref, values)
    spec.update(
        {
            "description": "Backend-computed score composition and evidence coverage.",
            "width": "container",
            "height": max(110, 28 * len(values)),
            "layer": [
                {
                    "mark": {"type": "bar", "cornerRadiusEnd": 3},
                    "encoding": {
                        "y": {
                            "field": "label",
                            "type": "nominal",
                            "sort": [category.title() for category in SCORE_ORDER],
                            "axis": {"title": None},
                        },
                        "x": {
                            "field": "score",
                            "type": "quantitative",
                            "scale": {"domain": [0, 100], "nice": False},
                            "axis": {"title": "Score", "tickCount": 5},
                        },
                        "opacity": {
                            "field": "coverage",
                            "type": "quantitative",
                            "scale": {"domain": [0, 1], "range": [0.35, 1]},
                            "legend": None,
                        },
                        "color": {
                            "field": "color",
                            "type": "nominal",
                            "scale": None,
                            "legend": None,
                        },
                        "tooltip": [
                            {"field": "label", "type": "nominal", "title": "Category"},
                            {"field": "score", "type": "quantitative", "title": "Score"},
                            {
                                "field": "coverage",
                                "type": "quantitative",
                                "format": ".0%",
                                "title": "Evidence coverage",
                            },
                        ],
                    },
                },
                {
                    "mark": {"type": "text", "align": "left", "dx": 5, "color": "#344054"},
                    "encoding": {
                        "y": {"field": "label", "type": "nominal", "sort": [category.title() for category in SCORE_ORDER]},
                        "x": {"field": "score", "type": "quantitative", "scale": {"domain": [0, 100]}},
                        "text": {"field": "score", "type": "quantitative", "format": ".0f"},
                    },
                },
            ],
        }
    )
    return ChartSpec(
        chart_id="score_composition",
        title="Score composition",
        code_ref=code_ref,
        spec=spec,
    )


def trend_chart(trends: dict[str, Any]) -> ChartSpec | None:
    code_ref = "urban_dossier_backend.chart_specs:trend_chart@1"
    values = []
    quarter_order: list[str] = []
    for signal in TREND_ORDER:
        series = (trends.get(signal) or {}).get("quarterly_series") or []
        for point in series[-20:]:
            quarter = point.get("quarter")
            count = point.get("count")
            if not isinstance(quarter, str):
                continue
            if quarter not in quarter_order:
                quarter_order.append(quarter)
            if not isinstance(count, (int, float)):
                continue
            values.append(
                {
                    "signal": signal,
                    "signal_label": TREND_LABELS[signal],
                    "quarter": quarter,
                    "count": round(float(count), 2),
                }
            )
    if not values:
        return None

    spec = _base_spec(code_ref, values)
    spec.update(
        {
            "description": "Quarter-keyed local signal history from backend trend payloads.",
            "width": "container",
            "height": 150,
            "mark": {"type": "line", "point": {"filled": True, "size": 24}, "strokeWidth": 1.5},
            "encoding": {
                "x": {
                    "field": "quarter",
                    "type": "ordinal",
                    "sort": quarter_order,
                    "axis": {"title": None, "labelAngle": -35},
                },
                "y": {
                    "field": "count",
                    "type": "quantitative",
                    "axis": {"title": "Observed count"},
                },
                "color": {
                    "field": "signal_label",
                    "type": "nominal",
                    "scale": {"range": ["#315f73", "#8c5a10", "#765b8a"]},
                    "legend": {"title": None, "orient": "top"},
                },
                "tooltip": [
                    {"field": "signal_label", "type": "nominal", "title": "Signal"},
                    {"field": "quarter", "type": "ordinal", "title": "Quarter"},
                    {"field": "count", "type": "quantitative", "title": "Count"},
                ],
            },
        }
    )
    return ChartSpec(
        chart_id="recent_trends",
        title="Quarterly signals",
        code_ref=code_ref,
        spec=spec,
    )


def score_distribution_chart(
    overview_context: dict[str, Any] | None,
    uncertainty: dict[str, Any] | None,
) -> ChartSpec | None:
    code_ref = "urban_dossier_backend.chart_specs:score_distribution_chart@1"
    overall = (overview_context or {}).get("overall") or {}
    overview_distribution = overall.get("distribution") or {}
    uncertainty_distribution = (uncertainty or {}).get("distribution") or {}
    # The interval artifact is H3 r9 while the public overview is H3 r8. Only
    # layer an interval when its own r9 histogram and marker are available;
    # otherwise fall back to the overview distribution without pretending the
    # two grains describe the same estimand.
    distribution = uncertainty_distribution or overview_distribution
    bins = distribution.get("bins") or []
    marker_score = distribution.get("marker_score")
    marker_percentile = distribution.get("marker_percentile")
    if not bins or not isinstance(marker_score, (int, float)):
        return None

    layers: list[dict[str, Any]] = []
    score_range = (
        (uncertainty or {}).get("score_range") or []
        if uncertainty_distribution
        else []
    )
    if (
        len(score_range) == 2
        and isinstance(score_range[0], (int, float))
        and isinstance(score_range[1], (int, float))
    ):
        layers.append(
            {
                "data": {
                    "values": [
                        {
                            "range_start": float(score_range[0]),
                            "range_end": float(score_range[1]),
                        }
                    ]
                },
                "mark": {"type": "rect", "color": "#765b8a", "opacity": 0.14},
                "encoding": {
                    "x": {
                        "field": "range_start",
                        "type": "quantitative",
                        "scale": {"domain": [0, 100], "nice": False},
                    },
                    "x2": {"field": "range_end"},
                    "tooltip": [
                        {
                            "field": "range_start",
                            "type": "quantitative",
                            "title": "Sensitivity low",
                        },
                        {
                            "field": "range_end",
                            "type": "quantitative",
                            "title": "Sensitivity high",
                        },
                    ],
                },
            }
        )
    layers.extend(
        [
            {
                "data": {"values": bins},
                "mark": {"type": "bar", "color": "#98a2b3", "opacity": 0.8},
                "encoding": {
                    "x": {
                        "field": "bin_start",
                        "type": "quantitative",
                        "bin": "binned",
                        "scale": {"domain": [0, 100], "nice": False},
                        "axis": {"title": "Overall score", "tickCount": 5},
                    },
                    "x2": {"field": "bin_end"},
                    "y": {
                        "field": "count",
                        "type": "quantitative",
                        "axis": {"title": "H3 cells", "tickMinStep": 1},
                    },
                    "tooltip": [
                        {"field": "bin_start", "type": "quantitative", "title": "From"},
                        {"field": "bin_end", "type": "quantitative", "title": "To"},
                        {"field": "count", "type": "quantitative", "title": "Cells"},
                    ],
                },
            },
            {
                "data": {
                    "values": [
                        {
                            "score": float(marker_score),
                            "percentile": marker_percentile,
                            "marker_label": "Center cell",
                        }
                    ]
                },
                "mark": {"type": "rule", "color": "#101828", "strokeWidth": 2},
                "encoding": {
                    "x": {
                        "field": "score",
                        "type": "quantitative",
                        "scale": {"domain": [0, 100], "nice": False},
                    },
                    "tooltip": [
                        {"field": "marker_label", "type": "nominal", "title": "Marker"},
                        {"field": "score", "type": "quantitative", "title": "Cell score"},
                        {
                            "field": "percentile",
                            "type": "quantitative",
                            "format": ".0%",
                            "title": "City percentile",
                        },
                    ],
                },
            },
        ]
    )

    spec = _base_spec(code_ref, [])
    spec.pop("data")
    grain = distribution.get("grain") or "unknown_cell_population"
    spec.update(
        {
            "description": (
                f"City distribution of {grain}; marker and any sensitivity "
                "range refer to the same containing cell and score method."
            ),
            "width": "container",
            "height": 145,
            "layer": layers,
        }
    )
    return ChartSpec(
        chart_id="score_distribution",
        title="City score distribution · containing cell",
        code_ref=code_ref,
        spec=spec,
    )


def compare_scores_chart(
    scores_a: dict[str, Any], scores_b: dict[str, Any], deltas: dict[str, Any]
) -> ChartSpec:
    code_ref = "urban_dossier_backend.chart_specs:compare_scores_chart@1"
    values = []
    for category in SCORE_ORDER:
        delta = deltas.get(category)
        for location, scores in (("Pinned", scores_a), ("Current", scores_b)):
            score = scores.get(category)
            if not isinstance(score, (int, float)):
                continue
            values.append(
                {
                    "category": category,
                    "label": category.replace("_", " ").title(),
                    "location": location,
                    "score": round(float(score), 2),
                    "delta_b_minus_a": round(float(delta), 2)
                    if isinstance(delta, (int, float))
                    else None,
                }
            )

    spec = _base_spec(code_ref, values)
    spec.update(
        {
            "description": "Two backend score payloads with the backend-computed B minus A delta.",
            "width": "container",
            "height": 170,
            "mark": {"type": "bar", "cornerRadiusTopLeft": 2, "cornerRadiusTopRight": 2},
            "encoding": {
                "x": {
                    "field": "label",
                    "type": "nominal",
                    "sort": [category.title() for category in SCORE_ORDER],
                    "axis": {"title": None, "labelAngle": -25},
                },
                "xOffset": {"field": "location"},
                "y": {
                    "field": "score",
                    "type": "quantitative",
                    "scale": {"domain": [0, 100], "nice": False},
                    "axis": {"title": "Score"},
                },
                "color": {
                    "field": "location",
                    "type": "nominal",
                    "scale": {"domain": ["Pinned", "Current"], "range": ["#98a2b3", "#315f73"]},
                    "legend": {"title": None, "orient": "top"},
                },
                "tooltip": [
                    {"field": "label", "type": "nominal", "title": "Category"},
                    {"field": "location", "type": "nominal", "title": "Location"},
                    {"field": "score", "type": "quantitative", "title": "Score"},
                    {
                        "field": "delta_b_minus_a",
                        "type": "quantitative",
                        "title": "Current − pinned",
                    },
                ],
            },
        }
    )
    return ChartSpec(
        chart_id="compare_scores",
        title="Score comparison",
        code_ref=code_ref,
        spec=spec,
    )


def detail_chart_specs(
    scores: dict[str, Any],
    coverage: dict[str, Any],
    trends: dict[str, Any],
    overview_context: dict[str, Any] | None = None,
    uncertainty: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    charts = [
        score_composition_chart(scores, coverage),
        score_distribution_chart(overview_context, uncertainty),
        trend_chart(trends),
    ]
    return {chart.chart_id: chart.model_dump() for chart in charts if chart is not None}
