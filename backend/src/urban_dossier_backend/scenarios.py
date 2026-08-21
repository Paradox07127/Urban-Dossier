"""Walking isochrones and intervention simulation.

Both back agent tools that previously raised NotImplementedError.

``walking_isochrone`` does real street-network routing over the pedestrian
graph published by ``backend/scripts/build_walking_graph.py``. Only the
neighbourhood around the origin is loaded per request, so the backend never
holds the 2.1M-node city graph resident.

``simulate_intervention`` projects a score change using the empirical
count->score curves fitted by ``backend/scripts/fit_intervention_elasticity.py``
and then re-aggregates through the *real* scoring functions. It is explicitly
correlational; every response says so.
"""

from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .categories import CATEGORY_CONFIG
from .config import CACHE_DIR, PRIORITY_DECAY, READY_DATA_DIR
from .secondary_scoring import compute_secondary_scores
from .utils import build_priority_weights

logger = logging.getLogger(__name__)


WALK_GRAPH_DIR = Path(
    os.getenv("URBAN_DOSSIER_WALK_GRAPH_DIR", "/mnt/data/urban-dossier-state/maps/walk")
)
# Average adult walking speed. Exposed in every response so a caller can see
# exactly what "10 minutes" was taken to mean.
DEFAULT_WALK_SPEED_MPS = 1.33
# Street routes are longer than straight lines, so the candidate box has to be
# wider than the crow-flies reach or the search gets clipped at the boundary.
BBOX_DETOUR_FACTOR = 1.45


# --------------------------------------------------------------------------- #
# Walking isochrone
# --------------------------------------------------------------------------- #


def _walk_graph_paths() -> tuple[Path, Path]:
    return WALK_GRAPH_DIR / "walk_nodes.parquet", WALK_GRAPH_DIR / "walk_edges.parquet"


def walk_graph_available() -> bool:
    nodes_path, edges_path = _walk_graph_paths()
    return nodes_path.exists() and edges_path.exists()


def _equal_area_m2(polygon) -> float:
    """Area in square metres via a local azimuthal equal-area projection."""

    try:
        from pyproj import Transformer
        from shapely.ops import transform as shapely_transform

        centroid = polygon.centroid
        transformer = Transformer.from_crs(
            "EPSG:4326",
            f"+proj=aeqd +lat_0={centroid.y} +lon_0={centroid.x} +units=m +datum=WGS84",
            always_xy=True,
        )
        return float(shapely_transform(transformer.transform, polygon).area)
    except Exception:  # noqa: BLE001 - area is informational, never fatal
        return float("nan")


def walking_isochrone(
    latitude: float,
    longitude: float,
    minutes: int = 10,
    walk_speed_mps: float = DEFAULT_WALK_SPEED_MPS,
    concave_ratio: float = 0.35,
) -> dict[str, Any]:
    """Street-network walking isochrone as a GeoJSON Feature.

    Contract (from ``tools._walking_isochrone``): a Feature with Polygon
    geometry and properties {minutes, area_m2, mode}. Extra provenance fields
    are added so the caller can see the speed, method and how much of the
    network was searched.
    """

    nodes_path, edges_path = _walk_graph_paths()
    if not walk_graph_available():
        return {
            "error": (
                "Walking graph not built. Expected "
                f"{nodes_path} and {edges_path}."
            ),
            "retry_hint": (
                "Run: python backend/scripts/build_walking_graph.py "
                "--pbf <NewYork.osm.pbf> --out "
                f"{WALK_GRAPH_DIR}"
            ),
        }

    budget_m = float(minutes) * 60.0 * float(walk_speed_mps)
    box_m = budget_m * BBOX_DETOUR_FACTOR
    lat_pad = box_m / 111_320.0
    lon_pad = box_m / (111_320.0 * max(math.cos(math.radians(latitude)), 0.01))

    import duckdb
    import networkx as nx

    con = duckdb.connect()
    try:
        node_rows = con.execute(
            f"SELECT node_id, lat, lon FROM read_parquet('{nodes_path.as_posix()}') "
            "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            [
                latitude - lat_pad,
                latitude + lat_pad,
                longitude - lon_pad,
                longitude + lon_pad,
            ],
        ).fetchall()

        if not node_rows:
            return {
                "error": "No pedestrian network near this point.",
                "retry_hint": "Check the coordinates are inside the built extent.",
            }

        coords = {int(r[0]): (float(r[1]), float(r[2])) for r in node_rows}

        # Pull only edges whose endpoints are both inside the candidate box.
        con.execute("CREATE TEMP TABLE local_nodes(node_id BIGINT)")
        con.executemany(
            "INSERT INTO local_nodes VALUES (?)", [(nid,) for nid in coords]
        )
        edge_rows = con.execute(
            f"SELECT u, v, length_m FROM read_parquet('{edges_path.as_posix()}') "
            "WHERE u IN (SELECT node_id FROM local_nodes) "
            "AND v IN (SELECT node_id FROM local_nodes)"
        ).fetchall()
    finally:
        con.close()

    if not edge_rows:
        return {
            "error": "Pedestrian nodes found but no connecting edges near this point.",
            "retry_hint": "The graph may have been built from a clipped extract.",
        }

    graph = nx.Graph()
    for u, v, length in edge_rows:
        u, v = int(u), int(v)
        length = float(length)
        # Keep the shortest parallel edge between a node pair.
        existing = graph.get_edge_data(u, v)
        if existing is None or length < existing["length_m"]:
            graph.add_edge(u, v, length_m=length)

    # Snap to the nearest graph node. Squared degrees are fine for ranking.
    origin = min(
        graph.nodes,
        key=lambda nid: (coords[nid][0] - latitude) ** 2 + (coords[nid][1] - longitude) ** 2,
    )
    origin_lat, origin_lon = coords[origin]
    snap_m = math.dist(
        (0.0, 0.0),
        (
            (origin_lat - latitude) * 111_320.0,
            (origin_lon - longitude) * 111_320.0 * math.cos(math.radians(latitude)),
        ),
    )

    distances = nx.single_source_dijkstra_path_length(
        graph, origin, cutoff=budget_m, weight="length_m"
    )
    reachable = [coords[nid] for nid in distances]

    if len(reachable) < 4:
        return {
            "error": (
                f"Only {len(reachable)} node(s) reachable within {minutes} minutes; "
                "cannot form a polygon."
            ),
            "retry_hint": "Try a longer duration or a point closer to the street network.",
        }

    from shapely import concave_hull
    from shapely.geometry import MultiPoint, mapping

    points = MultiPoint([(lon, lat) for lat, lon in reachable])
    hull = concave_hull(points, ratio=concave_ratio)
    if hull.geom_type not in ("Polygon", "MultiPolygon"):
        hull = points.convex_hull
    if hull.geom_type == "MultiPolygon":
        hull = max(hull.geoms, key=lambda g: g.area)

    return {
        "type": "Feature",
        "geometry": mapping(hull),
        "properties": {
            "minutes": int(minutes),
            "area_m2": round(_equal_area_m2(hull), 1),
            "mode": "walk",
            "method": "street_network_dijkstra",
            "walking_speed_mps": walk_speed_mps,
            "distance_budget_m": round(budget_m, 1),
            "reachable_nodes": len(reachable),
            "searched_nodes": graph.number_of_nodes(),
            "origin_snap_m": round(snap_m, 1),
            "concave_ratio": concave_ratio,
            "graph_source": str(WALK_GRAPH_DIR),
        },
    }


# --------------------------------------------------------------------------- #
# Intervention simulation
# --------------------------------------------------------------------------- #


def _elasticity_path() -> Path:
    override = os.getenv("URBAN_DOSSIER_ELASTICITY_PATH")
    if override:
        return Path(override)
    base = CACHE_DIR or Path("data/cache")
    return Path(base) / "simulation" / "elasticity.json"


@lru_cache(maxsize=1)
def _load_elasticity(path_str: str) -> dict[str, Any]:
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def _curve_lookup(curve: list[list[float]], slope: float | None, count: float) -> float:
    """Score at ``count`` from the fitted curve.

    Inside the observed range: linear interpolation between neighbouring
    points. Past the top: extrapolate along the fitted slope but never exceed
    100, and never fall below the last observed score -- the underlying scoring
    is monotone in count.
    """

    if not curve:
        return float("nan")
    if count <= curve[0][0]:
        return float(curve[0][1])
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if count <= x1:
            if x1 == x0:
                return float(y1)
            weight = (count - x0) / (x1 - x0)
            return float(y0 + weight * (y1 - y0))
    last_x, last_y = curve[-1]
    projected = last_y + (slope or 0.0) * (count - last_x)
    return float(min(100.0, max(last_y, projected)))


def simulate_intervention(
    latitude: float,
    longitude: float,
    intervention_type: str,
    count: int = 1,
    radius_m: int = 500,
    time_window_days: int = 365,
    priority_order: list[str] | None = None,
    data_mode: str | None = None,
) -> dict[str, Any]:
    """Project scores after adding ``count`` assets, using fitted curves.

    Contract (from ``tools._simulate_intervention``):
      {baseline_scores, projected_scores, deltas, affected_cells}

    Method: the asset is added to the H3 cell (or ZIP) containing the point.
    That unit's new count is mapped to a score through the fitted curve, the
    unit's score is substituted back into the same average the scoring layer
    uses, and the category and overall scores are recomputed by the real
    ``compute_secondary_scores``. Nothing here invents an aggregation.
    """

    path = _elasticity_path()
    if not path.exists():
        return {
            "error": f"Elasticity artifact not found at {path}.",
            "retry_hint": (
                "Run: python backend/scripts/fit_intervention_elasticity.py "
                "--ready-root data/ready"
            ),
        }

    artifact = _load_elasticity(str(path))
    entry = (artifact.get("interventions") or {}).get(intervention_type)
    if not entry:
        return {
            "error": f"Unknown intervention_type '{intervention_type}'.",
            "available": sorted((artifact.get("interventions") or {})),
        }
    if not entry.get("available"):
        return {
            "error": (
                f"No fitted curve for '{intervention_type}': {entry.get('reason')}"
            ),
        }

    from .service import _provider_from_mode, _normalize_priority_order

    provider, _resolved_mode = _provider_from_mode(data_mode)
    order = _normalize_priority_order(priority_order)
    # Same decay the detail path uses, so baseline_scores here match what
    # /api/analyze-point would report for the identical point.
    weights = build_priority_weights(order, PRIORITY_DECAY)

    point_payload = provider.get_point_signals(
        latitude, longitude, radius_m, time_window_days
    )
    baselines = provider.get_baselines()
    prepared = point_payload.get("prepared_scores") or {}
    current_state = point_payload.get("current_state") or {}

    baseline_scores = compute_secondary_scores(
        current_state, baselines, prepared, user_priority_weights=weights
    )

    category = entry["category"]
    sub_dataset = entry["sub_dataset"]
    grain = entry["grain"]
    count_column = entry["count_column"]
    score_table = READY_DATA_DIR / entry["score_table"]

    if not score_table.exists():
        return {"error": f"Score table missing at {score_table}."}

    import duckdb

    con = duckdb.connect()
    try:
        if grain == "zip":
            zip_code = ((point_payload.get("target") or {}).get("zip")) or (
                (current_state.get("target") or {}).get("zip")
                if isinstance(current_state.get("target"), dict)
                else None
            )
            if not zip_code:
                return {
                    "error": "Could not resolve the ZIP for this point; "
                    f"'{intervention_type}' is scored by ZIP."
                }
            unit_rows = con.execute(
                f'SELECT zip AS unit, "{count_column}" AS c, score AS s '
                f"FROM read_parquet('{score_table.as_posix()}') WHERE zip = ?",
                [str(zip_code)],
            ).fetchall()
            affected = [str(zip_code)]
            peer_rows = unit_rows
        else:
            import h3

            center = h3.latlng_to_cell(latitude, longitude, 9)
            # Use the exact same centre-distance filter as point scoring. A
            # raw grid_disk includes corner cells outside the requested radius
            # and made scenario deltas aggregate a different neighbourhood.
            cells = provider._h3_cells_for_radius(latitude, longitude, radius_m)
            placeholders = ", ".join(["?"] * len(cells))
            peer_rows = con.execute(
                f'SELECT h3_r9 AS unit, "{count_column}" AS c, score AS s '
                f"FROM read_parquet('{score_table.as_posix()}') "
                f"WHERE h3_r9 IN ({placeholders})",
                cells,
            ).fetchall()
            unit_rows = [r for r in peer_rows if r[0] == center]
            affected = [center]
    finally:
        con.close()

    curve = entry["curve"]
    slope = entry.get("linear_slope_per_unit")

    current_unit_count = float(unit_rows[0][1]) if unit_rows else 0.0
    projected_unit_count = current_unit_count + float(count)

    # Anchor on what this unit actually scores and apply only the curve's
    # *marginal* effect. Substituting the citywide mean outright would discard
    # a real measurement: one ZIP scores 43 with 42 parks while ZIPs with ~45
    # parks average 97, so the level difference is local character, not
    # something adding three parks would deliver.
    curve_now = _curve_lookup(curve, slope, current_unit_count)
    curve_after = _curve_lookup(curve, slope, projected_unit_count)
    marginal = curve_after - curve_now

    actual_unit_score = (
        float(unit_rows[0][2]) if unit_rows and unit_rows[0][2] is not None else None
    )
    if actual_unit_score is None:
        # Unit absent from the score table means no asset of this kind today;
        # the curve level is the only estimate available.
        projected_unit_score = curve_after
    else:
        projected_unit_score = min(100.0, max(0.0, actual_unit_score + marginal))

    # Substitute the affected unit's score into the same average the scoring
    # layer computes, so the projection cannot drift from real aggregation.
    peer_scores = [float(r[2]) for r in peer_rows if r[2] is not None]
    baseline_sub = prepared.get(category, {}).get(sub_dataset)

    others = [float(r[2]) for r in peer_rows if r[0] not in affected and r[2] is not None]
    projected_pool = others + [projected_unit_score]
    projected_sub = round(sum(projected_pool) / len(projected_pool)) if projected_pool else None

    projected_prepared = {cat: dict(subs) for cat, subs in prepared.items()}
    projected_prepared.setdefault(category, {})[sub_dataset] = projected_sub

    projected_scores = compute_secondary_scores(
        current_state, baselines, projected_prepared, user_priority_weights=weights
    )

    deltas = {
        key: round(float(projected_scores[key]) - float(baseline_scores[key]), 2)
        for key in baseline_scores
        if baseline_scores.get(key) is not None and projected_scores.get(key) is not None
    }

    spearman = entry.get("spearman_count_vs_score")
    weak = spearman is not None and spearman < 0.7
    result = {
        "baseline_scores": baseline_scores,
        "projected_scores": projected_scores,
        "deltas": deltas,
        "affected_cells": affected,
        "intervention": {
            "type": intervention_type,
            "count": int(count),
            "category": category,
            "sub_dataset": sub_dataset,
            "grain": grain,
        },
        "evidence": {
            "unit_count_before": current_unit_count,
            "unit_count_after": projected_unit_count,
            "unit_score_before": actual_unit_score,
            "unit_score_after": round(projected_unit_score, 2),
            "curve_marginal_effect": round(marginal, 2),
            "sub_score_before": baseline_sub,
            "sub_score_after": projected_sub,
            "peer_units_in_radius": len(peer_scores),
        },
        "method": artifact.get("method"),
        "causal": False,
        "caveat": artifact.get("caveat"),
        "fit": {
            "n_units": entry.get("n_units"),
            "spearman_count_vs_score": spearman,
            "observed_count_range": entry.get("count_range"),
            "quality": "weak" if weak else "strong",
        },
    }
    if weak:
        result["fit"]["warning"] = (
            f"Count explains little of the score for '{intervention_type}' "
            f"(Spearman {spearman}). Treat this projection as indicative only."
        )
    return result
