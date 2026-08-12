"""Deterministic GeoJSON and presentation contract for point comparisons.

The browser does not subtract scores or choose a semantic colour scale. It
selects one of the backend-published delta properties and applies the stops
shipped beside the geometry. This keeps the map, workbench and JSON on the
same ``point_b - point_a`` definition.
"""

from __future__ import annotations

import math
from typing import Any

from .metrics import METHODOLOGY_VERSION


DELTA_CATEGORIES = ("overall", "safety", "transit", "amenities", "building")
EARTH_RADIUS_M = 6_371_008.8

# ColorBrewer PuOr, with a quiet zero. Orange is negative (B lower), purple is
# positive (B higher); unlike red/green, both poles remain distinguishable for
# the common red-green colour-vision deficiencies.
DELTA_STOPS = (
    (-30, "#7f3b08"),
    (-20, "#b35806"),
    (-10, "#f1a340"),
    (0, "#f7f7f7"),
    (10, "#998ec3"),
    (20, "#542788"),
    (30, "#2d004b"),
)


def _circle(longitude: float, latitude: float, radius_m: int, vertices: int = 64) -> list[list[float]]:
    """Closed geodesic ring around a WGS84 point."""

    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    angular = radius_m / EARTH_RADIUS_M
    ring: list[list[float]] = []
    for index in range(vertices):
        bearing = 2 * math.pi * index / vertices
        lat2 = math.asin(
            math.sin(lat1) * math.cos(angular)
            + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
        )
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(angular) * math.cos(lat1),
            math.cos(angular) - math.sin(lat1) * math.sin(lat2),
        )
        ring.append([round(math.degrees(lon2), 7), round(math.degrees(lat2), 7)])
    ring.append(ring[0])
    return ring


def _properties(
    role: str,
    deltas: dict[str, float],
    scores_a: dict[str, Any],
    scores_b: dict[str, Any],
) -> dict[str, Any]:
    values: dict[str, Any] = {"role": role, "direction": "point_b_minus_point_a"}
    for category in DELTA_CATEGORIES:
        values[f"{category}_delta"] = deltas.get(category)
        values[f"{category}_score_a"] = scores_a.get(category)
        values[f"{category}_score_b"] = scores_b.get(category)
    return values


def comparison_delta_map(
    point_a: dict[str, Any],
    point_b: dict[str, Any],
    radius_m: int,
    deltas: dict[str, float],
    scores_a: dict[str, Any],
    scores_b: dict[str, Any],
) -> dict[str, Any]:
    """Build the server-owned spatial comparison contract."""

    lat_a, lon_a = float(point_a["latitude"]), float(point_a["longitude"])
    lat_b, lon_b = float(point_b["latitude"]), float(point_b["longitude"])
    shared = _properties("comparison", deltas, scores_a, scores_b)

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[*_circle(lon_a, lat_a, radius_m)]]},
            "properties": {**shared, "kind": "comparison_area", "role": "point_a"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[*_circle(lon_b, lat_b, radius_m)]]},
            "properties": {**shared, "kind": "comparison_area", "role": "point_b"},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon_a, lat_a], [lon_b, lat_b]],
            },
            "properties": {**shared, "kind": "comparison_connector"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_a, lat_a]},
            "properties": {**shared, "kind": "comparison_point", "role": "point_a"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon_b, lat_b]},
            "properties": {**shared, "kind": "comparison_point", "role": "point_b"},
        },
    ]

    all_coordinates = [coord for feature in features[:2] for coord in feature["geometry"]["coordinates"][0]]
    longitudes = [coord[0] for coord in all_coordinates]
    latitudes = [coord[1] for coord in all_coordinates]

    return {
        "schema_version": "1.0",
        "code_ref": "urban_dossier_backend.comparison_maps:comparison_delta_map@1",
        "methodology_version": METHODOLOGY_VERSION,
        "direction": "point_b_minus_point_a",
        "radius_m": radius_m,
        "bbox": [min(longitudes), min(latitudes), max(longitudes), max(latitudes)],
        "geojson": {"type": "FeatureCollection", "features": features},
        "presentation": {
            "palette": "ColorBrewer PuOr 7",
            "domain": [-30, 30],
            "clamp": True,
            "stops": [{"value": value, "color": color} for value, color in DELTA_STOPS],
            "zero_color": "#f7f7f7",
            "no_data_color": "#98a2b3",
            "point_a_color": "#101828",
            "category_fields": {
                "general": "overall_delta",
                **{category: f"{category}_delta" for category in DELTA_CATEGORIES if category != "overall"},
            },
        },
    }
