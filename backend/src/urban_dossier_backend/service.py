from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from .categories import CATEGORY_CONFIG, DEFAULT_PRIORITY_ORDER, signal_to_category_map
from .chart_specs import compare_scores_chart, detail_chart_specs
from .comparison_maps import comparison_delta_map
from .config import URBAN_DOSSIER_DATA_MODE, PRIORITY_DECAY
from .evidence import build_evidence, extract_why_now, verify_priority_actions
from .pattern_detector import detect_multi_signal_patterns
from .priority_engine import compute_priority_actions
from .providers.base import DataProvider
from .providers.direct_provider import DirectQueryDataProvider
from .providers.skill_provider import SkillDataProvider
from .report import generate_action_brief
from .secondary_scoring import compute_scores_with_coverage
from .risk_flags import building_risk_flag
from .uncertainty import score_uncertainty
from .trend_engine import compute_all_trends
from .gpu_accel import get_gpu_status
from .utils import build_priority_weights


SCHEMA_VERSION = "v3.7.8"
CATEGORY_ALIASES = {
    "amenities": "amenities",
    "facilities": "amenities",
    "transit": "transit",
    "traffic": "transit",
    "safety": "safety",
    "building": "building",
    "general": "overall",
    "overall": "overall",
}


def _provider_from_mode(mode: str | None = None) -> tuple[DataProvider, str]:
    requested = (mode or URBAN_DOSSIER_DATA_MODE or "direct").lower()
    if requested == "skill":
        return SkillDataProvider(), "skill"
    if requested == "auto":
        skill = SkillDataProvider()
        if skill.get_coverage().get("provider_ready"):
            return skill, "skill"
        return DirectQueryDataProvider(), "direct"
    return DirectQueryDataProvider(), "direct"


def _normalize_category_id(category_id: str | None) -> str | None:
    if category_id is None:
        return None
    normalized = CATEGORY_ALIASES.get(category_id.strip().lower())
    return normalized


def _normalize_priority_order(priority_order: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for category in priority_order or []:
        canonical = _normalize_category_id(category)
        if canonical in CATEGORY_CONFIG and CATEGORY_CONFIG[canonical]["detail_rankable"] and canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        normalized = list(DEFAULT_PRIORITY_ORDER)
    return normalized


def get_categories_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "default_order": DEFAULT_PRIORITY_ORDER,
        "overview_tags": ["general", "safety", "transit", "amenities"],
        "aliases": {
            "general": "overall",
            "overall": "overall",
            "facilities": "amenities",
            "traffic": "transit",
        },
        "categories": [
            {
                "category_id": category_id,
                "label": config["label"],
                "map_driving": config["map_driving"],
                "detail_rankable": config["detail_rankable"],
                "signals": config["signals"],
            }
            for category_id, config in CATEGORY_CONFIG.items()
        ],
    }


def get_coverage_payload(data_mode: str | None = None) -> dict[str, Any]:
    provider, resolved_mode = _provider_from_mode(data_mode)
    coverage = provider.get_coverage()
    coverage["schema_version"] = SCHEMA_VERSION
    coverage["resolved_data_mode"] = resolved_mode
    return coverage


def get_health_payload(data_mode: str | None = None) -> dict[str, Any]:
    provider, resolved_mode = _provider_from_mode(data_mode)
    coverage = provider.get_coverage()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "resolved_data_mode": resolved_mode,
        "provider": coverage.get("provider"),
        "provider_ready": coverage.get("provider_ready", True),
        "overview_ready": coverage.get("overview_ready", False),
        "available_datasets": coverage.get("available_datasets", []),
        "gpu": get_gpu_status(),
    }


def get_overview_payload(
    view_mode: str,
    category_id: str | None,
    viewport: dict | None,
    zoom: int | None,
    data_mode: str | None = None,
) -> dict[str, Any]:
    provider, resolved_mode = _provider_from_mode(data_mode)
    normalized_view_mode = "overall" if _normalize_category_id(category_id) == "overall" else view_mode
    normalized_category = None if normalized_view_mode == "overall" else _normalize_category_id(category_id)
    payload = provider.get_overview_layer(normalized_view_mode, normalized_category, viewport, zoom)
    payload["schema_version"] = SCHEMA_VERSION
    payload["resolved_data_mode"] = resolved_mode
    return payload


def _build_detail_payload(
    latitude: float,
    longitude: float,
    radius_m: int,
    priority_order: list[str],
    time_window_days: int,
    data_mode: str | None = None,
) -> dict[str, Any]:
    provider, resolved_mode = _provider_from_mode(data_mode)
    normalized_order = _normalize_priority_order(priority_order)
    priority_weights = build_priority_weights(normalized_order, PRIORITY_DECAY)

    from concurrent.futures import ThreadPoolExecutor

    # Run the 4 independent data-fetch stages in parallel
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_signals = pool.submit(provider.get_point_signals, latitude, longitude, radius_m, time_window_days)
        f_overview = pool.submit(provider.get_overview_context, latitude, longitude)
        f_baselines = pool.submit(provider.get_baselines)
        f_historical = pool.submit(provider.get_local_timeseries, latitude, longitude, radius_m, time_window_days)

    point_payload = f_signals.result()
    current_state = point_payload["current_state"]
    detail_items = point_payload["detail_items"]
    overview_context = f_overview.result()
    baselines = f_baselines.result()
    historical = f_historical.result()
    trends = compute_all_trends(current_state, historical, baselines)
    patterns = detect_multi_signal_patterns(current_state, trends)
    priority_actions = compute_priority_actions(
        current_state=current_state,
        trends=trends,
        baselines=baselines,
        priority_weights=priority_weights,
        signal_to_category=signal_to_category_map(),
    )
    evidence_table = build_evidence(point_payload.get("query_evidence", []), trends, patterns)
    verified_actions = verify_priority_actions(priority_actions, evidence_table)
    why_now = extract_why_now(trends, patterns)
    scores, score_coverage = compute_scores_with_coverage(
        current_state,
        baselines,
        point_payload.get("prepared_scores"),
        user_priority_weights=priority_weights,
    )
    # Individual published indicators remain visible even when they
    # deliberately carry zero category weight (for example HVI). This avoids
    # inventing a cross-grain category composite merely to get a context value
    # through the API. Values have already passed each table's publication gate
    # in the provider.
    metric_scores = {
        metric_id: value
        for category_scores in (point_payload.get("prepared_scores") or {}).values()
        for metric_id, value in category_scores.items()
    }

    # Hotspot detection (GPU DBSCAN)
    hotspots = []
    try:
        from .hotspot_engine import detect_hotspots
        # Collect all incidents with coordinates from detail_items
        all_incidents = []
        for inc in detail_items.get("recent_incidents", []):
            if inc.get("latitude") and inc.get("longitude"):
                all_incidents.append(inc)
        # Also include building flags with coordinates
        for flag in detail_items.get("building_flags", []):
            if flag.get("latitude") and flag.get("longitude"):
                all_incidents.append({**flag, "kind": "building_violation"})

        if all_incidents:
            hotspots = detect_hotspots(
                all_incidents,
                eps_meters=80,  # ~1 city block
                min_samples=3,
            )
    except Exception as exc:
        logger.warning("Hotspot detection failed (non-fatal): %s", exc)

    detail_items["hotspots"] = hotspots
    uncertainty_payload = score_uncertainty(latitude, longitude)
    chart_specs = detail_chart_specs(
        scores,
        score_coverage,
        trends,
        overview_context,
        uncertainty_payload,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "detail",
        "data_mode": resolved_mode,
        "target": point_payload["target"],
        "priority_profile": {
            "order": normalized_order,
            "weights": priority_weights,
        },
        "priority_actions": verified_actions,
        "why_now": why_now,
        "current_state": current_state,
        "detail_items": detail_items,
        "overview_context": overview_context,
        "trends": trends,
        "patterns": patterns,
        "evidence_table": evidence_table,
        "data_gaps": point_payload.get("data_gaps", []),
        "scores": scores,
        "metric_scores": metric_scores,
        "chart_specs": chart_specs,
        # How much of each category's intended evidence base was actually
        # present. Sits beside `scores` rather than inside it so the score
        # contract is unchanged, and so a consumer that ignores coverage keeps
        # working -- while one that reads it can stop presenting a one-source
        # score as though it were a five-source one.
        "score_coverage": score_coverage,
        # And how firm the composite is under the assumptions behind it: 95%
        # intervals from the offline 1,000-draw sensitivity analysis, served
        # at the grain it was computed at (the containing cell). None when the
        # artifact has not been generated -- absent uncertainty is disclosed
        # as absent, never faked as a point estimate's confidence.
        "score_uncertainty": uncertainty_payload,
        # P0-02's answer: building risk as an absolute, threshold-based flag
        # beside the relative scores. unknown means no data, never "no risk".
        "building_risk_flag": building_risk_flag(current_state),
        "baselines": baselines,
        "enriched_context": point_payload.get("enriched_context", {}),
    }


def preview_point(
    latitude: float,
    longitude: float,
    radius_m: int,
    priority_order: list[str],
    time_window_days: int,
    data_mode: str | None = None,
) -> dict[str, Any]:
    payload = _build_detail_payload(
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        priority_order=priority_order,
        time_window_days=time_window_days,
        data_mode=data_mode,
    )
    payload["mode"] = "detail_preview"
    payload["preview_ready"] = True
    return payload


def analyze_point(
    latitude: float,
    longitude: float,
    radius_m: int,
    priority_order: list[str],
    time_window_days: int,
    data_mode: str | None = None,
    use_llm: bool = True,
    report_mode: str = "individual",
) -> dict[str, Any]:
    payload = _build_detail_payload(
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        priority_order=priority_order,
        time_window_days=time_window_days,
        data_mode=data_mode,
    )
    payload["report_mode"] = report_mode
    summary, brief = generate_action_brief(payload, use_llm=use_llm, report_mode=report_mode)
    payload["report_summary"] = summary
    payload["report_markdown"] = brief
    return payload


def search_address_payload(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the PLUTO / location index for addresses matching the query.

    Factored out of the inline ``/api/search`` handler in ``app.py`` so the
    in-process agent dispatcher can reuse the same logic without going over
    HTTP loopback. Returns
    ``{"results": [{address, borough, zip, latitude, longitude}, ...]}``.
    """

    from .providers.direct_provider import DirectQueryDataProvider

    cleaned_query = (query or "").strip().upper()
    if not cleaned_query or len(cleaned_query) < 3:
        return {"results": []}

    provider = DirectQueryDataProvider()
    # NOTE: _connect() hands back the thread's cached connection -- never
    # close it here. This function used to close it in a finally block,
    # poisoning every later query on the same worker thread.
    con = provider._connect()
    capped_limit = max(1, min(int(limit or 5), 10))
    results: list[dict[str, Any]] = []
    for source_name in ("location_index", "pluto"):
        # Ready-first for the location index: after the processed/ -> ready/
        # layout migration the processed dir is empty on fresh deployments,
        # and /api/search silently returned zero results for every query --
        # after which the agent guessed coordinates from model memory,
        # exactly the failure this tool exists to prevent (found 2026-08-13
        # by the business eval). The column introspection below absorbs the
        # ready/processed schema differences.
        source = provider.ready_source_sql(source_name) or provider._source_sql(
            source_name
        )
        if not source:
            continue
        try:
            # The ready location index and raw PLUTO disagree on column
            # names (matched_address vs address, zip vs postcode). Resolve
            # against the file's real schema instead of assuming one -- the
            # hardcoded names silently emptied /api/search for every query
            # when the ready index took over (its Binder error was swallowed
            # by this loop's best-effort except).
            columns = {
                row[0]
                for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
            }
            address_col = next(
                (c for c in ("address", "matched_address") if c in columns), None
            )
            zip_col = next((c for c in ("postcode", "zip") if c in columns), None)
            if not address_col or not {"latitude", "longitude"} <= columns:
                continue
            zip_select = f'"{zip_col}" AS zip' if zip_col else "NULL AS zip"
            sql = f"""
                SELECT "{address_col}" AS address, borough, {zip_select},
                       latitude, longitude
                FROM {source}
                WHERE upper(coalesce("{address_col}", '')) LIKE ?
                  AND latitude IS NOT NULL AND longitude IS NOT NULL
                LIMIT {capped_limit}
            """
            rows = provider._query_rows(con, sql, [f"%{cleaned_query}%"])
            for row in rows:
                lat = row.get("latitude")
                lon = row.get("longitude")
                if lat and lon:
                    results.append(
                        {
                            "address": row.get("address", ""),
                            "borough": row.get("borough", ""),
                            "zip": row.get("zip", ""),
                            "latitude": float(lat),
                            "longitude": float(lon),
                        }
                    )
            if results:
                break
        except Exception:  # noqa: BLE001 - best-effort fallback across sources
            continue
    return {"results": results}


def run_watchlist(
    seeds: list[dict[str, Any]],
    radius_m: int,
    time_window_days: int,
    priority_order: list[str] | None = None,
    data_mode: str | None = None,
) -> dict[str, Any]:
    order = _normalize_priority_order(priority_order or list(DEFAULT_PRIORITY_ORDER))
    items = []
    for seed in seeds:
        latitude = seed.get("latitude")
        longitude = seed.get("longitude")
        if latitude is None or longitude is None:
            continue
        result = analyze_point(
            latitude=float(latitude),
            longitude=float(longitude),
            radius_m=radius_m,
            priority_order=order,
            time_window_days=time_window_days,
            data_mode=data_mode,
            use_llm=False,
        )
        items.append(
            {
                "seed": seed,
                "top_priority": result["priority_actions"][0]["action"] if result["priority_actions"] else None,
                "top_priority_score": result["priority_actions"][0]["priority_score"] if result["priority_actions"] else 0.0,
                "overall_score": result["scores"].get("overall"),
                "priority_count": len(result["priority_actions"]),
                "result": result,
            }
        )
    items.sort(key=lambda item: (item["top_priority_score"], item["priority_count"]), reverse=True)

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "watchlist",
        "fixed_priority_order": order,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Agent tool endpoints
#
# The urban-dossier-analyst tools declared these contracts long before the
# endpoints existed and raised NotImplementedError with the required request /
# response schema in the message. The shapes below match those contracts
# exactly so the tool layer needs no translation.
# --------------------------------------------------------------------------- #


def compare_points(
    point_a: dict[str, float],
    point_b: dict[str, float],
    radius_m: int = 500,
    priority_order: list[str] | None = None,
    time_window_days: int = 365,
    data_mode: str | None = None,
) -> dict[str, Any]:
    """Score two locations and report the per-category delta.

    Contract (from ``tools._compare_neighborhoods``):
      response {point_a: <analyze-point payload>, point_b: <...>,
                deltas: {category_id: float}}

    ``deltas`` is b - a, so a positive value means point_b scores higher on
    that category. Categories missing from either side are omitted rather than
    defaulted to zero -- a missing score is not the same as "no difference".
    """

    order = _normalize_priority_order(priority_order)

    def _score(point: dict[str, float]) -> dict[str, Any]:
        return analyze_point(
            latitude=float(point["latitude"]),
            longitude=float(point["longitude"]),
            radius_m=radius_m,
            priority_order=order,
            time_window_days=time_window_days,
            data_mode=data_mode,
            use_llm=False,
            report_mode="individual",
        )

    payload_a = _score(point_a)
    payload_b = _score(point_b)

    scores_a = payload_a.get("scores") or {}
    scores_b = payload_b.get("scores") or {}
    deltas: dict[str, float] = {}
    for category in sorted(set(scores_a) | set(scores_b)):
        value_a = scores_a.get(category)
        value_b = scores_b.get(category)
        if value_a is None or value_b is None:
            continue
        deltas[category] = round(float(value_b) - float(value_a), 2)

    compare_chart = compare_scores_chart(scores_a, scores_b, deltas)
    delta_map = comparison_delta_map(
        payload_a.get("target") or point_a,
        payload_b.get("target") or point_b,
        radius_m,
        deltas,
        scores_a,
        scores_b,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "compare",
        "point_a": payload_a,
        "point_b": payload_b,
        "deltas": deltas,
        "delta_map": delta_map,
        "chart_specs": {compare_chart.chart_id: compare_chart.model_dump()},
        "radius_m": radius_m,
        "priority_order": order,
    }


def query_dataset_rows(
    dataset_id: str,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Filtered row query against a published ready Parquet dataset.

    Contract (from ``tools._query_dataset``):
      response {dataset_id: str, columns: list[str], rows: list[dict], total: int}

    ``total`` is the number of rows matching the filters *before* the limit, so
    the agent can tell "only 5 rows exist" from "showing the first 5 of 900".

    Filter values are bound as query parameters. Column names cannot be bound,
    so they are validated against the file's real schema and quoted -- several
    NYC columns contain spaces (e.g. ``ZIP CODE``), which makes rejecting them
    outright too strict.
    """

    from .config import READY_DATA_DIR
    from .providers.direct_provider import READY_DATASET_PATHS

    normalized = (dataset_id or "").strip().lower()
    relative = READY_DATASET_PATHS.get(normalized)
    if relative is None:
        return {
            "error": f"Unknown dataset_id '{dataset_id}'.",
            "available_datasets": sorted(READY_DATASET_PATHS),
            "dataset_id": dataset_id,
            "columns": [],
            "rows": [],
            "total": 0,
        }

    path = READY_DATA_DIR / relative
    if not path.exists():
        return {
            "error": f"Dataset '{normalized}' is registered but not published at {path}.",
            "retry_hint": "Run the ready-Parquet publication step for this dataset.",
            "dataset_id": normalized,
            "columns": [],
            "rows": [],
            "total": 0,
        }

    import duckdb

    con = duckdb.connect()
    try:
        source = f"read_parquet('{path.as_posix()}')"
        # DESCRIBE yields (column_name, column_type, ...) -- index 0, not 1.
        # Reading the type column here silently turned every filter into an
        # "unknown column" and returned the dataset unfiltered.
        columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]

        where_parts: list[str] = []
        params: list[Any] = []
        unknown: list[str] = []
        for column, value in (filters or {}).items():
            if column not in columns:
                unknown.append(column)
                continue
            quoted = '"' + str(column).replace('"', '""') + '"'
            if isinstance(value, (list, tuple, set)):
                values = list(value)
                if not values:
                    continue
                placeholders = ", ".join(["?"] * len(values))
                where_parts.append(f"{quoted} IN ({placeholders})")
                params.extend(values)
            elif isinstance(value, dict):
                # Range semantics: the agent naturally reaches for
                # {"min": a, "max": b} when asked to count within bounds.
                # Anything else dict-shaped used to fall into the scalar
                # branch and blow up DuckDB (HTTP 500); reject it with a
                # structured error the model can act on instead.
                bounds = {k: v for k, v in value.items() if k in ("min", "max")}
                if not bounds or set(value) - {"min", "max"} or any(
                    isinstance(v, (dict, list, tuple, set)) for v in bounds.values()
                ):
                    return {
                        "error": (
                            f"Unsupported filter value for '{column}': {value!r}. "
                            "Filters accept a scalar (equality), a list "
                            "(membership), or {'min': a, 'max': b} (range)."
                        ),
                        "retry_hint": "Re-issue with a supported filter shape.",
                        "dataset_id": normalized,
                        "columns": columns,
                        "rows": [],
                        "total": 0,
                    }
                if "min" in bounds:
                    where_parts.append(f"{quoted} >= ?")
                    params.append(bounds["min"])
                if "max" in bounds:
                    where_parts.append(f"{quoted} <= ?")
                    params.append(bounds["max"])
            else:
                where_parts.append(f"{quoted} = ?")
                params.append(value)

        where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
        total = con.execute(
            f"SELECT count(*) FROM {source}{where_sql}", params
        ).fetchone()[0]

        cursor = con.execute(
            f"SELECT * FROM {source}{where_sql} LIMIT {int(limit)}", params
        )
        row_columns = [col[0] for col in cursor.description]
        rows = [dict(zip(row_columns, record)) for record in cursor.fetchall()]
    finally:
        con.close()

    payload: dict[str, Any] = {
        "dataset_id": normalized,
        "columns": columns,
        "rows": rows,
        "total": int(total),
        "limit": int(limit),
        "source": relative,
    }
    if unknown:
        # Surface rather than silently ignore: a typo'd filter would otherwise
        # look like a legitimately unfiltered result.
        payload["ignored_filters"] = unknown
        payload["ignored_filters_note"] = (
            "These filter keys are not columns in this dataset and were not "
            "applied. Check `columns` for valid names."
        )
    return payload
