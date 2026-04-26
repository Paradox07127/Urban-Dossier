#!/usr/bin/env python3
"""extract_highlights.py — Extract poster-relevant highlights from Urban Dossier analysis JSON.

Usage:
    python3 scripts/extract_highlights.py <analysis.json> [--output highlights.json] [--demo]

Reads the full Urban Dossier analysis JSON (output of _build_detail_payload()) and
produces a compact highlights JSON suitable for poster template rendering.
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEMO_CACHE_DIR = SCRIPT_DIR / "demo_cache"


def score_label_and_color(score):
    """Derive human-readable label and color from an overall score (0-100)."""
    if score is None:
        return "N/A", "#565e74"
    if score >= 80:
        return "Excellent", "#006d4a"
    if score >= 60:
        return "Good", "#0053dc"
    if score >= 40:
        return "Fair", "#c59a1a"
    return "Needs Attention", "#9f403d"


ICON_MAP = {
    "safety": "shield",
    "transit": "bus",
    "amenities": "tree",
    "building": "building",
}

EMOJI_MAP = {
    "shield": "\U0001f6e1\ufe0f",
    "bus": "\U0001f68c",
    "tree": "\U0001f333",
    "building": "\U0001f3e2",
}


# Map dimension names to the signal-level trend keys that feed them.
# The trends dict uses signal names (e.g. "collision") not dimension names (e.g. "transit").
DIMENSION_TREND_KEYS = {
    "safety": ["rodent", "sanitation", "safety"],
    "transit": ["collision", "transit"],
    "amenities": ["tree", "restaurant", "amenities"],
    "building": ["violation", "building"],
}


def get_trend(trends, dimension):
    """Extract trend direction for a dimension from the trends dict.

    Checks the dimension name directly first, then falls back to
    signal-level keys that map to that dimension.
    """
    if not trends:
        return "stable"
    # Direct match on dimension name
    if dimension in trends:
        t = trends[dimension]
        if isinstance(t, dict):
            return t.get("direction", "stable")
    # Check signal-level keys mapped to this dimension
    for key in DIMENSION_TREND_KEYS.get(dimension, []):
        if key in trends:
            t = trends[key]
            if isinstance(t, dict):
                return t.get("direction", "stable")
    return "stable"


def build_top_findings(current_state, enriched_context, baselines):
    """Build the top_findings list from current_state, enriched_context, and baselines."""
    findings = []

    # Transit: collision count
    transit = current_state.get("transit", {}) if current_state else {}
    collision_count = transit.get("collision_count_500m")
    if collision_count is not None:
        collision_baselines = baselines.get("collision", {}) if baselines else {}
        p50 = collision_baselines.get("p50")
        if p50 is not None:
            if collision_count > p50:
                context = f"Above city median ({p50})"
            else:
                context = f"Below city median ({p50})"
        else:
            context = "Within study area"
        findings.append({
            "label": "Traffic Crashes",
            "value": str(collision_count),
            "context": context,
        })

    # Amenities: tree count
    amenities = current_state.get("amenities", {}) if current_state else {}
    tree_count = amenities.get("tree_count_500m")
    if tree_count is not None:
        tree_baselines = baselines.get("tree", {}) if baselines else {}
        p75 = tree_baselines.get("p75")
        if p75 is not None and tree_count >= p75:
            context = "Top 25% citywide"
        else:
            context = "Within study area"
        findings.append({
            "label": "Street Trees",
            "value": str(tree_count),
            "context": context,
        })

    # Enriched: A-grade restaurants
    enriched = enriched_context or {}
    restaurant_highlights = enriched.get("restaurant_highlights", {})
    a_grade = restaurant_highlights.get("a_grade_count")
    if a_grade is not None:
        findings.append({
            "label": "A-Grade Restaurants",
            "value": str(a_grade),
            "context": "Healthy dining options",
        })

    # Safety: rodent positive
    safety = current_state.get("safety", {}) if current_state else {}
    rodent = safety.get("rodent_positive_500m")
    if rodent is not None and len(findings) < 3:
        findings.append({
            "label": "Rodent Reports",
            "value": str(rodent),
            "context": "Active inspections nearby",
        })

    # Safety: sanitation 311
    sanitation = safety.get("sanitation_311_recent_count")
    if sanitation is not None and len(findings) < 3:
        findings.append({
            "label": "Sanitation Complaints",
            "value": str(sanitation),
            "context": "Recent 311 reports",
        })

    # Building: open class C violations
    building = current_state.get("building", {}) if current_state else {}
    class_c = building.get("open_class_c_250m")
    if class_c is not None and len(findings) < 3:
        findings.append({
            "label": "Building Violations",
            "value": str(class_c),
            "context": "Open Class C violations",
        })

    # Amenities: restaurant count
    restaurant_count = amenities.get("restaurant_count_500m")
    if restaurant_count is not None and len(findings) < 3:
        findings.append({
            "label": "Nearby Restaurants",
            "value": str(restaurant_count),
            "context": "Within 500m radius",
        })

    # Transit: subway count
    subway = transit.get("subway_count")
    if subway is not None and len(findings) < 3:
        findings.append({
            "label": "Subway Stations",
            "value": str(subway),
            "context": "Within walking distance",
        })

    return findings[:3]


def extract_highlights(data):
    """Extract poster highlights from a full Urban Dossier analysis dict."""
    target = data.get("target", {})
    scores = data.get("scores", {})
    current_state = data.get("current_state", {})
    enriched_context = data.get("enriched_context", {})
    baselines = data.get("baselines", {})
    trends = data.get("trends", {})
    priority_actions = data.get("priority_actions", [])

    overall = scores.get("overall")
    label, color = score_label_and_color(overall)

    dimension_scores = []
    for dim in ("safety", "transit", "amenities", "building"):
        s = scores.get(dim)
        if s is not None:
            dimension_scores.append({
                "name": dim.capitalize(),
                "score": s,
                "icon": ICON_MAP.get(dim, dim),
                "trend": get_trend(trends, dim),
            })

    top_findings = build_top_findings(current_state, enriched_context, baselines)

    priority_action = None
    if priority_actions and len(priority_actions) > 0:
        top_action = priority_actions[0]
        if isinstance(top_action, dict):
            priority_action = top_action.get("action")

    return {
        "location_name": target.get("matched_address"),
        "borough": target.get("borough"),
        "zip": target.get("zip"),
        "overall_score": overall,
        "score_label": label,
        "score_color": color,
        "dimension_scores": dimension_scores,
        "top_findings": top_findings,
        "priority_action": priority_action,
        "data_source": "NYC Open Data",
        "generated_date": date.today().isoformat(),
        "radius_m": target.get("radius_m"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract poster-relevant highlights from Urban Dossier analysis JSON."
    )
    parser.add_argument(
        "analysis_json",
        help="Path to the Urban Dossier analysis JSON file",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output path for highlights JSON (default: stdout)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Check demo_cache/ for pre-computed results",
    )
    args = parser.parse_args()

    input_basename = os.path.basename(args.analysis_json)

    # Demo mode: check cache first
    if args.demo:
        cache_file = DEMO_CACHE_DIR / f"{input_basename}.highlights.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                cached["_demo_cached"] = True
            result_str = json.dumps(cached, indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(result_str + "\n", encoding="utf-8")
                print(f"Demo cache hit: {cache_file}", file=sys.stderr)
            else:
                print(result_str)
            return

    # Read input
    input_path = Path(args.analysis_json)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in {input_path}: {e}", file=sys.stderr)
        sys.exit(1)

    highlights = extract_highlights(data)
    result_str = json.dumps(highlights, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(result_str + "\n", encoding="utf-8")
        print(f"Highlights written to {args.output}", file=sys.stderr)
    else:
        print(result_str)


if __name__ == "__main__":
    main()
