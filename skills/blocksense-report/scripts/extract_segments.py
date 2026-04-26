#!/usr/bin/env python3
"""extract_segments.py — Split Urban Dossier analysis JSON into per-dimension segments.

Reads the full analysis payload (from _build_detail_payload() in service.py) and
produces a segments.json with one entry per dimension: safety, transit, amenities,
building. Each segment contains only that dimension's metrics, trends, enriched
context, and related priority actions.

Usage:
    python3 scripts/extract_segments.py <analysis.json> [--output segments.json] [--demo]

If --output is omitted, JSON is written to stdout.
If --demo is passed, checks scripts/demo_cache/ for a cached result first.

Exit codes:
    0 — success
    1 — error (file not found, invalid JSON, etc.)
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Signal-to-dimension mapping
# ---------------------------------------------------------------------------

SIGNAL_DIMENSION = {
    # safety
    "rodent": "safety",
    "311_sanitation": "safety",
    "ems_response": "safety",
    "fire_response": "safety",
    # transit
    "collision": "transit",
    "subway": "transit",
    "bus": "transit",
    "bike_routes": "transit",
    "open_streets": "transit",
    # amenities
    "trees": "amenities",
    "restaurants": "amenities",
    "parks": "amenities",
    "linknyc": "amenities",
    "toilets": "amenities",
    "facilities": "amenities",
    # building
    "housing_violations": "building",
    "aep": "building",
}

# Metric key → (label, unit, signal_name for baseline lookup)
METRIC_MAP = {
    "safety": [
        ("rodent_positive_500m", "Rodent Activity", "sites", "rodent"),
        ("sanitation_311_recent_count", "311 Sanitation Complaints", "complaints", "311_sanitation"),
        ("ems_avg_response_seconds", "EMS Response Time", "seconds", "ems_response"),
        ("fire_avg_response_seconds", "Fire Response Time", "seconds", "fire_response"),
    ],
    "transit": [
        ("collision_count_500m", "Traffic Collisions", "collisions", "collision"),
        ("subway_count", "Subway Stations", "stations", "subway"),
        ("bus_count", "Bus Stops", "stops", "bus"),
        ("bike_route_km", "Bike Routes", "km", "bike_routes"),
        ("open_streets_count", "Open Streets", "streets", "open_streets"),
    ],
    "amenities": [
        ("tree_count_500m", "Street Trees", "trees", "trees"),
        ("restaurant_count_500m", "Restaurants", "restaurants", "restaurants"),
        ("park_count", "Parks", "parks", "parks"),
        ("linknyc_count", "LinkNYC Kiosks", "kiosks", "linknyc"),
        ("toilet_count", "Public Toilets", "toilets", "toilets"),
        ("facility_count", "Public Facilities", "facilities", "facilities"),
    ],
    "building": [
        ("open_class_c_250m", "Open Class C Violations", "violations", "housing_violations"),
        ("aep_buildings", "AEP Buildings", "buildings", "aep"),
    ],
}

# Enriched context keys per dimension
ENRICHED_MAP = {
    "safety": ["complaint_breakdown", "collision_time_buckets"],
    "transit": ["collision_time_buckets"],
    "amenities": ["nearest_parks", "restaurant_highlights", "tree_health", "facility_types"],
    "building": ["violation_age"],
}


def _baseline_annotation(value, baseline):
    """Compute a human-readable annotation comparing value to baseline percentiles."""
    if baseline is None or not isinstance(baseline, dict):
        return None
    p25 = baseline.get("p25")
    p50 = baseline.get("p50")
    p75 = baseline.get("p75")
    if p25 is None or p50 is None or p75 is None:
        return None
    try:
        value = float(value)
        p25 = float(p25)
        p50 = float(p50)
        p75 = float(p75)
    except (TypeError, ValueError):
        return None

    if value > p75 * 1.5:
        return "WELL ABOVE P75"
    elif value > p75:
        return "Above P75"
    elif value > p50:
        return "Above median, approaching P75"
    elif value == p50:
        return "At city median"
    elif value >= p25:
        return "Below median, above P25"
    elif value >= p25 * 0.5:
        return "Below P25"
    else:
        return "WELL BELOW P25"


def _extract_metrics(current_state_dim, baselines, dim_key):
    """Extract metrics for a single dimension."""
    metrics = []
    if current_state_dim is None:
        return metrics
    for metric_key, label, unit, signal_name in METRIC_MAP.get(dim_key, []):
        value = current_state_dim.get(metric_key)
        if value is None:
            continue
        baseline = baselines.get(signal_name) if baselines else None
        annotation = _baseline_annotation(value, baseline)
        metrics.append({
            "label": label,
            "value": value,
            "unit": unit,
            "baseline": baseline,
            "annotation": annotation,
        })
    return metrics


def _extract_trends(trends_data, dim_key):
    """Extract trends belonging to a dimension."""
    if not trends_data:
        return []
    result = []
    for signal_name, trend_obj in trends_data.items():
        if SIGNAL_DIMENSION.get(signal_name) != dim_key:
            continue
        if not isinstance(trend_obj, dict):
            continue
        anomaly_data = trend_obj.get("anomaly", {}) or {}
        result.append({
            "signal": signal_name,
            "direction": trend_obj.get("direction", "unknown"),
            "delta_pct": trend_obj.get("recent_delta_pct"),
            "is_anomaly": anomaly_data.get("is_anomaly", False),
            "anomaly_context": anomaly_data.get("context"),
        })
    return result


def _extract_enriched(enriched_context, dim_key):
    """Extract enriched context fields for a dimension."""
    if not enriched_context:
        return {}
    result = {}
    for key in ENRICHED_MAP.get(dim_key, []):
        val = enriched_context.get(key)
        if val is not None:
            result[key] = val
    return result


def _extract_related_actions(priority_actions, dim_key):
    """Extract priority actions related to a dimension's signals."""
    if not priority_actions:
        return []
    dim_signals = {s for s, d in SIGNAL_DIMENSION.items() if d == dim_key}
    result = []
    for action in priority_actions:
        if not isinstance(action, dict):
            continue
        if action.get("signal") in dim_signals:
            result.append({
                "action": action.get("action", ""),
                "priority_score": action.get("priority_score"),
            })
    return result


def extract_segments(data):
    """Main extraction logic. Returns the segments dict."""
    target = data.get("target", {}) or {}
    scores = data.get("scores", {}) or {}
    current_state = data.get("current_state", {}) or {}
    trends = data.get("trends", {}) or {}
    baselines = data.get("baselines", {}) or {}
    enriched_context = data.get("enriched_context", {}) or {}
    patterns = data.get("patterns", []) or []
    priority_actions = data.get("priority_actions", []) or []
    why_now = data.get("why_now", []) or []
    data_gaps = data.get("data_gaps", []) or []

    location = {
        "name": target.get("matched_address", "Unknown"),
        "borough": target.get("borough", "Unknown"),
        "zip": target.get("zip", ""),
        "radius_m": target.get("radius_m", 500),
    }

    segments = []
    for dim_key in ("safety", "transit", "amenities", "building"):
        segment = {
            "dimension": dim_key,
            "score": scores.get(dim_key),
            "metrics": _extract_metrics(current_state.get(dim_key), baselines, dim_key),
            "trends": _extract_trends(trends, dim_key),
            "enriched": _extract_enriched(enriched_context, dim_key),
            "related_actions": _extract_related_actions(priority_actions, dim_key),
        }
        segments.append(segment)

    # Top 5 priority actions
    top_actions = []
    for action in sorted(priority_actions, key=lambda a: a.get("rank", 999))[:5]:
        if isinstance(action, dict):
            top_actions.append(action)

    return {
        "location": location,
        "overall_score": scores.get("overall"),
        "segments": segments,
        "cross_signal_patterns": patterns,
        "priority_actions": top_actions,
        "why_now": why_now,
        "data_gaps": data_gaps,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Split Urban Dossier analysis JSON into per-dimension segments."
    )
    parser.add_argument(
        "analysis_json",
        help="Path to the analysis JSON file from Urban Dossier.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path for segments JSON. Default: stdout.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Check scripts/demo_cache/ for a cached result before running.",
    )
    args = parser.parse_args()

    input_path = Path(args.analysis_json)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Demo cache check
    if args.demo:
        script_dir = Path(__file__).resolve().parent
        cache_dir = script_dir / "demo_cache"
        cache_file = cache_dir / f"{input_path.name}.segments.json"
        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8")
            if args.output:
                Path(args.output).write_text(cached, encoding="utf-8")
                print(f"[demo] Wrote cached segments to {args.output}", file=sys.stderr)
            else:
                sys.stdout.write(cached)
            sys.exit(0)

    # Read and parse input
    try:
        raw = input_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {input_path}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading {input_path}: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract segments
    result = extract_segments(data)

    # Output
    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output_json + "\n", encoding="utf-8")
        print(f"Segments written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output_json + "\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
