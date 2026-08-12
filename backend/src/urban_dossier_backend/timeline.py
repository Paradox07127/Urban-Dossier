"""Period-keyed H3 timeline publication for MapLibre global state."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import h3

from .metrics import METHODOLOGY_VERSION
from .periods import canonical_quarter, current_quarter, quarter_index
from .presentation import _category_rows, quantile_breaks
from .providers.direct_provider import DirectQueryDataProvider
from .utils import to_float


TIMELINE_SIGNALS = {
    "collision": ("safety/collisions_quarterly_h3.parquet", "Collisions"),
    "rodent": ("safety/rodent_quarterly_h3.parquet", "Positive rodent inspections"),
    "311_sanitation": ("safety/311_quarterly_h3.parquet", "Sanitation 311 requests"),
    "housing_violations": (
        "building/housing_violations_quarterly_h3.parquet",
        "Housing violations",
    ),
}
TIMELINE_COLORS = ["#f1eef6", "#bdc9e1", "#74a9cf", "#2b8cbe", "#045a8d"]


def _property_suffix(period: str) -> str:
    return period.replace("-", "_")


def _class_colors(class_count: int) -> list[str]:
    if class_count <= 1:
        return [TIMELINE_COLORS[0]]
    last = len(TIMELINE_COLORS) - 1
    indexes = [round(index * last / (class_count - 1)) for index in range(class_count)]
    return [TIMELINE_COLORS[index] for index in indexes]


def _empty(signal: str, label: str, artifact_version: str | None = None) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [],
        "metadata": {
            "schema_version": "1.0",
            "code_ref": "urban_dossier_backend.timeline:timeline_geojson@1",
            "methodology_version": METHODOLOGY_VERSION,
            "signal": signal,
            "label": label,
            "available": False,
            "artifact_version": artifact_version,
            "periods": [],
            "default_period": None,
            "population": "land_clipped_safety_h3_r8_cells",
            "cell_count": 0,
            "no_data_color": "#C6CACE",
        },
    }


def timeline_geojson(signal: str = "collision", limit_periods: int = 20) -> dict[str, Any]:
    if signal not in TIMELINE_SIGNALS:
        raise ValueError(f"signal must be one of {', '.join(TIMELINE_SIGNALS)}")
    if not 2 <= limit_periods <= 40:
        raise ValueError("limit_periods must be between 2 and 40")
    relative_path, label = TIMELINE_SIGNALS[signal]
    provider = DirectQueryDataProvider()
    path = provider._ready_path(relative_path)
    if not path.exists():
        return _empty(signal, label)
    stat = path.stat()
    artifact_version = f"{stat.st_size}:{stat.st_mtime_ns}"
    return _timeline_cached(signal, limit_periods, artifact_version, str(path))


@lru_cache(maxsize=16)
def _timeline_cached(
    signal: str,
    limit_periods: int,
    artifact_version: str,
    artifact_path: str,
) -> dict[str, Any]:
    _, label = TIMELINE_SIGNALS[signal]
    provider = DirectQueryDataProvider()
    con = provider._connect()
    path = Path(artifact_path)
    raw_periods = provider._query_rows(
        con,
        f"SELECT DISTINCT quarter FROM read_parquet('{path.as_posix()}')",
    )
    period_to_artifact_key: dict[str, str] = {}
    for row in raw_periods:
        raw = row.get("quarter")
        period = canonical_quarter(raw)
        if period is not None and isinstance(raw, str):
            period_to_artifact_key[period] = raw
    periods = sorted(period_to_artifact_key, key=quarter_index)[-limit_periods:]
    if not periods:
        return _empty(signal, label, artifact_version)

    artifact_keys = [period_to_artifact_key[period] for period in periods]
    placeholders = ", ".join(["?"] * len(artifact_keys))
    rows = provider._query_rows(
        con,
        f"""
            SELECT h3_r9, quarter, sum(try_cast(count AS DOUBLE)) AS value
            FROM read_parquet('{path.as_posix()}')
            WHERE quarter IN ({placeholders})
            GROUP BY h3_r9, quarter
        """,
        artifact_keys,
    )

    overview_rows = _category_rows("safety")
    geometry_by_cell = {
        row.get("h3") or row.get("cell_id"): row
        for row in overview_rows
        if row.get("h3") or row.get("cell_id")
    }
    values_by_cell: dict[str, dict[str, float]] = {
        cell: {period: 0.0 for period in periods} for cell in geometry_by_cell
    }
    dropped_r9_rows = 0
    for row in rows:
        period = canonical_quarter(row.get("quarter"))
        value = to_float(row.get("value"))
        r9 = row.get("h3_r9")
        if period not in period_to_artifact_key or value is None or not isinstance(r9, str):
            continue
        try:
            r8 = h3.cell_to_parent(r9, 8)
        except (TypeError, ValueError):
            dropped_r9_rows += 1
            continue
        if r8 not in values_by_cell:
            dropped_r9_rows += 1
            continue
        values_by_cell[r8][period] += value

    period_contracts = []
    presentation_by_period: dict[str, tuple[list[float], list[str]]] = {}
    for period in periods:
        population = [by_period[period] for by_period in values_by_cell.values()]
        breaks = quantile_breaks(population, 5)
        colors = _class_colors(len(breaks) + 1)
        suffix = _property_suffix(period)
        presentation_by_period[period] = (breaks, colors)
        period_contracts.append(
            {
                "period": period,
                "period_complete": period != current_quarter(),
                "value_property": f"v_{suffix}",
                "color_property": f"c_{suffix}",
                "breaks": breaks,
                "colors": colors,
                "classification": "quantile",
                "requested_classes": 5,
                "effective_classes": len(colors),
                "population_n": len(population),
                "total_value": round(sum(population), 2),
            }
        )

    features = []
    for cell, geometry_row in geometry_by_cell.items():
        properties: dict[str, Any] = {
            "h3": cell,
            "land_fraction": geometry_row.get("land_fraction"),
        }
        for period in periods:
            value = values_by_cell[cell][period]
            breaks, colors = presentation_by_period[period]
            class_index = sum(value >= edge for edge in breaks)
            suffix = _property_suffix(period)
            properties[f"v_{suffix}"] = round(value, 2)
            properties[f"c_{suffix}"] = colors[class_index]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": (
                        "MultiPolygon"
                        if geometry_row.get("boundary_type") == "MultiPolygon"
                        else "Polygon"
                    ),
                    "coordinates": geometry_row.get("boundary"),
                },
                "properties": properties,
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "schema_version": "1.0",
            "code_ref": "urban_dossier_backend.timeline:timeline_geojson@1",
            "methodology_version": METHODOLOGY_VERSION,
            "signal": signal,
            "label": label,
            "available": bool(features),
            "artifact_version": artifact_version,
            "periods": period_contracts,
            "default_period": periods[-1],
            "population": "land_clipped_safety_h3_r8_cells",
            "cell_count": len(features),
            "dropped_r9_rows_outside_population": dropped_r9_rows,
            "no_data_color": "#C6CACE",
            "animation": {
                "state_property": "timeline_period",
                "lookup": "period-keyed MapLibre match expression",
                "tick_mutation": "setGlobalStateProperty",
            },
        },
    }
