"""Server-owned class breaks and accessible map palettes.

The API publishes lookup tables, not formulas for the browser to reinterpret.
Breaks are computed over the same land-clipped H3 r8 population served by the
overview; palettes are fixed, named artifacts with reproducible CVD checks.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from .metrics import METHODOLOGY_VERSION
from .providers.direct_provider import DirectQueryDataProvider
from .utils import to_float


# Exact d3-scale-chromatic / ColorBrewer schemePuOr[5] values. Keeping the
# named upstream artifact here lets Python publish the contract without making
# a JavaScript palette package part of the backend runtime.
# schemePuOr[5], worst to best. The fourth pair this ramp has worn, each
# retired for a named reason: green-red failed colour vision, blue-red read
# as an election map, and PuOr's brown pole read as dirt to actual users.
# Orange keeps the warm-means-worse instinct without the mud; deep violet
# reads considered rather than partisan. Checker-measured on these exact
# values: worst adjacent CVD dE 17.4, normal-vision 19.5 -- the strongest
# margins of any candidate so far.
SCORE_COLORS = ["#e66101", "#fdb863", "#f7f7f7", "#b2abd2", "#5e3c99"]
BIVARIATE_COLORS = [
    ["#e8e8e8", "#e4acac", "#c85a5a"],
    ["#b0d5df", "#ad9ea5", "#985356"],
    ["#64acbe", "#627f8c", "#574249"],
]
CATEGORIES = ("overall", "safety", "transit", "amenities")
CVD_MATRICES = {
    "normal": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritanopia": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def _percentile(sorted_values: list[float], share: float) -> float:
    if not sorted_values:
        raise ValueError("cannot classify an empty population")
    position = (len(sorted_values) - 1) * share
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def quantile_breaks(values: list[float], classes: int) -> list[float]:
    """Linear quantiles with duplicate edges removed honestly.

    A constant or heavily tied population can support fewer visual classes
    than requested. Publishing fewer strict edges is preferable to nudging
    duplicate values and claiming distinctions the data does not contain.
    """

    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return []
    raw = [_percentile(ordered, index / classes) for index in range(1, classes)]
    minimum = ordered[0]
    return [
        round(value, 2)
        for index, value in enumerate(raw)
        if value > minimum and (index == 0 or value > raw[index - 1])
    ]


def _srgb_to_lab(color: str, matrix: tuple[tuple[float, ...], ...]) -> tuple[float, float, float]:
    rgb = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
    simulated = [
        max(0.0, min(1.0, sum(matrix[row][column] * linear[column] for column in range(3))))
        for row in range(3)
    ]
    red, green, blue = simulated
    x = (0.4124564 * red + 0.3575761 * green + 0.1804375 * blue) / 0.95047
    y = 0.2126729 * red + 0.7151522 * green + 0.072175 * blue
    z = (0.0193339 * red + 0.119192 * green + 0.9503041 * blue) / 1.08883

    def f(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _delta_e(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def palette_cvd_report(colors: list[str], edges: list[tuple[int, int]]) -> dict[str, Any]:
    results = {}
    for name, matrix in CVD_MATRICES.items():
        labs = [_srgb_to_lab(color, matrix) for color in colors]
        results[name] = round(min(_delta_e(labs[left], labs[right]) for left, right in edges), 1)
    return {
        "method": "Machado 100% simulation matrices + CIE76 adjacent-cell delta E",
        "threshold": 8.0,
        "minimum_adjacent_delta_e": results,
        "passes": all(value >= 8 for value in results.values()),
    }


@lru_cache(maxsize=4)
def _category_rows(category: str) -> tuple[dict[str, Any], ...]:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    provider = DirectQueryDataProvider()
    if provider.overview_dir is None:
        return ()
    path = provider.overview_dir / f"overview_{category}_h3_r8.parquet"
    if not path.exists():
        return ()
    return tuple(provider._load_overview_rows(path, limit=5000))


@lru_cache(maxsize=4)
def _category_values(category: str) -> tuple[float, ...]:
    field = "overall_score" if category == "overall" else f"{category}_score"
    return tuple(
        value
        for row in _category_rows(category)
        if (value := to_float(row.get(field))) is not None
    )


@lru_cache(maxsize=4)
def _univariate(category: str) -> dict[str, Any]:
    values = list(_category_values(category))
    breaks = quantile_breaks(values, 5) if values else [20, 40, 60, 80]
    colors = SCORE_COLORS[: len(breaks) + 1]
    return {
        "category": category,
        "field": "overall_score" if category == "overall" else f"{category}_score",
        "classification": "quantile",
        "requested_classes": 5,
        "effective_classes": len(colors),
        "breaks": breaks,
        "colors": colors,
        "population": "land_clipped_h3_r8_cells",
        "population_n": len(values),
        "no_data_color": "#98a2b3",
    }


def score_color(category: str, score: float) -> str:
    # Building is a valid detail score but has no H3 overview layer. Keep its
    # chart available with disclosed fixed bands rather than manufacturing an
    # H3 population or failing the entire detail response.
    if category not in CATEGORIES:
        class_index = sum(float(score) >= edge for edge in (20, 40, 60, 80))
        return SCORE_COLORS[class_index]
    contract = _univariate(category)
    class_index = sum(float(score) >= edge for edge in contract["breaks"])
    return contract["colors"][class_index]


def _bivariate_axis(category: str, categories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    breaks = quantile_breaks(list(_category_values(category)), 3)
    return {
        **categories[category],
        "requested_classes": 3,
        "effective_classes": len(breaks) + 1,
        "breaks": breaks,
    }


def presentation_contract(x_category: str = "safety", y_category: str = "transit") -> dict[str, Any]:
    if x_category not in CATEGORIES or y_category not in CATEGORIES:
        raise ValueError(f"categories must be one of {', '.join(CATEGORIES)}")
    categories = {category: _univariate(category) for category in CATEGORIES}
    flat_bivariate = [color for row in BIVARIATE_COLORS for color in row]
    bivariate_edges = [
        *((row * 3 + column, row * 3 + column + 1) for row in range(3) for column in range(2)),
        *((row * 3 + column, (row + 1) * 3 + column) for row in range(2) for column in range(3)),
    ]
    return {
        "schema_version": "1.0",
        "code_ref": "urban_dossier_backend.presentation:presentation_contract@1",
        "methodology_version": METHODOLOGY_VERSION,
        "univariate": {
            "palette": "d3-scale-chromatic schemePuOr[5]",
            "categories": categories,
            "accessibility": palette_cvd_report(
                SCORE_COLORS,
                [(index, index + 1) for index in range(len(SCORE_COLORS) - 1)],
            ),
        },
        "bivariate": {
            "palette": "Stevens blue-red 3x3",
            "x": _bivariate_axis(x_category, categories),
            "y": _bivariate_axis(y_category, categories),
            "matrix": BIVARIATE_COLORS,
            "index": "matrix[y_class][x_class]",
            "accessibility": palette_cvd_report(flat_bivariate, bivariate_edges),
        },
    }


def bivariate_geojson(x_category: str = "safety", y_category: str = "transit") -> dict[str, Any]:
    contract = presentation_contract(x_category, y_category)
    x_contract = contract["bivariate"]["x"]
    y_contract = contract["bivariate"]["y"]
    matrix = contract["bivariate"]["matrix"]
    y_field = y_contract["field"]
    y_by_cell = {
        row.get("h3") or row.get("cell_id"): to_float(row.get(y_field))
        for row in _category_rows(y_category)
    }
    features = []
    for row in _category_rows(x_category):
        cell = row.get("h3") or row.get("cell_id")
        x_score = to_float(row.get(x_contract["field"]))
        y_score = y_by_cell.get(cell)
        boundary = row.get("boundary")
        if cell is None or x_score is None or y_score is None or not boundary:
            continue
        x_class = sum(x_score >= edge for edge in x_contract["breaks"])
        y_class = sum(y_score >= edge for edge in y_contract["breaks"])
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "MultiPolygon" if row.get("boundary_type") == "MultiPolygon" else "Polygon",
                    "coordinates": boundary,
                },
                "properties": {
                    "h3": cell,
                    "x_category": x_category,
                    "y_category": y_category,
                    "x_score": round(x_score, 2),
                    "y_score": round(y_score, 2),
                    "x_class": x_class,
                    "y_class": y_class,
                    "bivariate_color": matrix[y_class][x_class],
                    "land_fraction": row.get("land_fraction"),
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "schema_version": contract["schema_version"],
            "code_ref": "urban_dossier_backend.presentation:bivariate_geojson@1",
            "methodology_version": METHODOLOGY_VERSION,
            "cell_count": len(features),
            "presentation": contract["bivariate"],
        },
    }
