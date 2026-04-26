from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from .categories import CATEGORY_CONFIG, DEFAULT_PRIORITY_ORDER, signal_to_category_map
from .config import URBAN_DOSSIER_DATA_MODE, PRIORITY_DECAY
from .evidence import build_evidence, extract_why_now, verify_priority_actions
from .pattern_detector import detect_multi_signal_patterns
from .priority_engine import compute_priority_actions
from .providers.base import DataProvider
from .providers.direct_provider import DirectQueryDataProvider
from .providers.skill_provider import SkillDataProvider
from .report import generate_action_brief
from .secondary_scoring import compute_secondary_scores
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
    scores = compute_secondary_scores(current_state, baselines, point_payload.get("prepared_scores"), user_priority_weights=priority_weights)

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
    con = provider._connect()
    capped_limit = max(1, min(int(limit or 5), 10))
    results: list[dict[str, Any]] = []
    try:
        for source_name in ("location_index", "pluto"):
            source = provider._source_sql(source_name)
            if not source:
                continue
            try:
                sql = f"""
                    SELECT address, borough, postcode as zip, latitude, longitude, BBL
                    FROM {source}
                    WHERE upper(coalesce(address, '')) LIKE ?
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
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001 - close should never raise upward
            pass
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
