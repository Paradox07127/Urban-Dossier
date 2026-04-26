from __future__ import annotations

import logging
import os
import re
from typing import Any

from .categories import signal_to_category_map
from .config import DEFAULT_MODEL, DEFAULT_OPENAI_API_KEY, DEFAULT_OPENAI_BASE_URL
from .utils import first_sentence

logger = logging.getLogger(__name__)

# Maps incident 'kind' values to their parent category. Used by the per-category
# prompt builder to select relevant incidents from the mixed incident list.
_INCIDENT_TO_CATEGORY = {
    "collision": "transit", "transit": "transit",
    "safety": "safety", "rodent": "safety", "311": "safety", "sanitation": "safety",
    "toilet": "amenities", "linknyc": "amenities", "restaurant": "amenities", "park": "amenities",
    "violation": "building", "building": "building",
}

# Each entry: (label, state_section, state_key, baseline_key, unit, higher_is_worse).
# Used by _build_category_prompt to attach inline baseline comparisons.
_BASELINE_METRICS = [
    ("Collision count", "transit", "collision_count_500m", "collision", "", True),
    ("Rodent positive", "safety", "rodent_positive_500m", "rodent", "", True),
    ("311 sanitation complaints", "safety", "sanitation_311_recent_count", "311_sanitation", "", True),
    ("EMS response (ZIP)", "safety", "ems_avg_response_seconds", "ems_response", "s", True),
    ("Fire response (ZIP)", "safety", "fire_avg_response_seconds", "fire_response", "s", True),
    ("Open Class C violations", "building", "open_class_c_250m", "housing_violations", "", True),
    ("Street trees", "amenities", "tree_count_500m", "trees", "", False),
    ("Restaurant count", "amenities", "restaurant_count_500m", "restaurants", "", False),
]


def fallback_brief(payload: dict, report_mode: str = "individual") -> str:
    """Produce a human-readable governance brief without LLM."""
    target = payload.get("target", {})
    radius = target.get("radius_m", 500)
    borough = target.get("borough") or "the selected area"
    zip_code = target.get("zip")
    scores = payload.get("scores", {})
    overall = scores.get("overall")
    actions = payload.get("priority_actions", [])
    why_now = payload.get("why_now", [])
    current = payload.get("current_state", {})
    data_gaps = payload.get("data_gaps", [])
    enriched = payload.get("enriched_context", {})

    location_desc = target.get("matched_address") or (f"{borough} (ZIP {zip_code})" if zip_code else borough)

    if report_mode == "organization":
        title = f"## Area Governance Assessment -- {location_desc}"
    else:
        title = f"## Your Neighborhood at a Glance -- {location_desc}"

    lines = [title, ""]

    if overall is not None:
        lines.append(f"Within the selected {radius}m radius, the overall outlook scores **{overall}/100**.")
    lines.append("")

    if actions:
        lines.append("### Priority issues")
        for item in actions[:3]:
            lines.append(f"- **{item['action']}** (priority score {item.get('priority_score', '?')})")
        lines.append("")

    if why_now:
        lines.append("### Why now")
        for item in why_now[:3]:
            lines.append(f"- {item['signal']}")
        lines.append("")

    lines.append("### Key findings")
    safety = current.get("safety", {})
    transit = current.get("transit", {})
    amenities = current.get("amenities", {})
    building = current.get("building", {})

    if report_mode == "individual":
        # Friendly, livability-focused
        if transit.get("collision_count_500m"):
            lines.append(f"- **{transit['collision_count_500m']}** traffic crashes recorded nearby -- be cautious at intersections")
        if safety.get("rodent_positive_500m"):
            lines.append(f"- **{safety['rodent_positive_500m']}** locations with confirmed rodent activity within {radius}m")
        if building.get("open_class_c_250m"):
            lines.append(f"- **{building['open_class_c_250m']}** open Class C housing violations nearby (urgent issues like no heat, lead paint, or pests)")
        if amenities.get("tree_count_500m"):
            lines.append(f"- **{amenities['tree_count_500m']}** street trees within {radius}m")
        parks = enriched.get("nearest_parks", [])
        if parks:
            lines.append(f"- Nearby parks: {', '.join(parks[:3])}")
        highlights = enriched.get("restaurant_highlights", {})
        if highlights.get("a_grade_count"):
            names = highlights.get("sample_a_names", [])
            name_str = f" including {', '.join(names[:2])}" if names else ""
            lines.append(f"- **{highlights['a_grade_count']}** restaurants with A health grade nearby{name_str}")
    else:
        # Operational, data-driven
        if transit.get("collision_count_500m"):
            lines.append(f"- **{transit['collision_count_500m']}** collision records within {radius}m (NYC DOT data)")
        if safety.get("ems_avg_response_seconds"):
            lines.append(f"- EMS average response: **{safety['ems_avg_response_seconds']}s** for ZIP {zip_code} (FDNY dispatch)")
        if building.get("open_class_c_250m"):
            age = enriched.get("violation_age", {})
            age_str = f", avg age {age['avg_age_days']} days" if age.get("avg_age_days") else ""
            lines.append(f"- **{building['open_class_c_250m']}** open Class C violations within 250m{age_str}")
        breakdown = enriched.get("complaint_breakdown", {})
        if breakdown:
            top_types = ", ".join(f"{k} ({v})" for k, v in list(breakdown.items())[:3])
            lines.append(f"- 311 complaint mix: {top_types}")
        highlights = enriched.get("restaurant_highlights", {})
        if highlights.get("total_graded"):
            lines.append(f"- Restaurant compliance: {highlights['a_grade_count']}/{highlights['total_graded']} A-graded ({round(highlights['a_grade_count']/highlights['total_graded']*100)}%)")

    if not any([transit.get("collision_count_500m"), safety.get("rodent_positive_500m"), building.get("open_class_c_250m")]):
        lines.append("- Limited public records found for this radius.")
    lines.append("")

    # Anomalies
    trends = payload.get("trends", {})
    anomalies = [(sig, t["anomaly"]) for sig, t in trends.items() if t.get("anomaly", {}).get("is_anomaly")]
    if anomalies:
        lines.append("### Statistical anomalies")
        for sig, anom in anomalies:
            z = anom.get("z_score")
            z_str = f" (z-score {z:+.2f})" if z is not None else ""
            lines.append(f"- **{sig}**{z_str}: {anom.get('context', '')}")
        lines.append("")

    # Patterns
    patterns = payload.get("patterns", [])
    if patterns:
        lines.append("### Cross-signal patterns")
        for pat in patterns:
            lines.append(f"- **{pat['title']}**: {pat.get('summary', '')}")
        lines.append("")

    if any(v is not None for v in [scores.get("safety"), scores.get("transit"), scores.get("amenities")]):
        lines.append("### Scores")
        for dim in ["safety", "transit", "amenities", "building"]:
            val = scores.get(dim)
            if val is not None:
                lines.append(f"- **{dim.title()}**: {val}/100")
        lines.append("")

    if data_gaps:
        lines.append("### Data limitations")
        for gap in data_gaps[:3]:
            lines.append(f"- {gap}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Staged report generation: one LLM call per category + synthesis
# ---------------------------------------------------------------------------

def _baseline_one_liner(value: Any, baseline: dict, unit: str = "", higher_is_worse: bool = True) -> str | None:
    """Return a concise baseline comparison, or None if data is missing."""
    if value is None or not baseline:
        return None
    v = float(value)
    p50 = baseline.get("p50")
    if p50 is None:
        return None
    if higher_is_worse:
        p75 = baseline.get("p75")
        if p75 is not None and v > p75:
            return f"{v:.0f}{unit} — among the worst ~25% citywide (median {p50})"
        if v > p50:
            return f"{v:.0f}{unit} — above city median of {p50}"
        return f"{v:.0f}{unit} — at or below city median of {p50}"
    p25 = baseline.get("p25")
    if p25 is not None and v < p25:
        return f"{v:.0f}{unit} — among the lowest ~25% citywide (median {p50})"
    if v < p50:
        return f"{v:.0f}{unit} — below city median of {p50}"
    return f"{v:.0f}{unit} — at or above city median of {p50}"


def _build_category_prompt(payload: dict, category: str, report_mode: str) -> str:
    """Build a small, focused prompt for one category."""
    target = payload.get("target", {})
    radius = target.get("radius_m", 500)
    zip_code = target.get("zip", "unknown")
    location_desc = target.get("matched_address") or target.get("borough", "NYC")
    scores = payload.get("scores", {})
    current_state = payload.get("current_state", {})
    baselines = payload.get("baselines", {})
    enriched = payload.get("enriched_context", {})
    trends = payload.get("trends", {})
    detail_items = payload.get("detail_items", {})

    cat_score = scores.get(category)
    cat_state = current_state.get(category, {})

    lines = [f"Location: {location_desc}, {radius}m radius, ZIP {zip_code}"]
    if cat_score is not None:
        lines.append(f"{category.title()} score: {cat_score}/100")
    lines.append("")

    # Raw metrics + inline baseline comparisons
    if cat_state:
        lines.append("Data:")
        for key, val in cat_state.items():
            if val is not None:
                lines.append(f"  {key}: {val}")
    for label, section, state_key, bl_key, unit, hiw in _BASELINE_METRICS:
        if section != category:
            continue
        val = cat_state.get(state_key)
        bl = baselines.get(bl_key, baselines.get(f"amenities_{bl_key}", {}))
        summary = _baseline_one_liner(val, bl, unit, hiw)
        if summary:
            lines.append(f"  Baseline — {label}: {summary}")

    # Category-specific enriched context (compact)
    if category == "safety":
        breakdown = enriched.get("complaint_breakdown", {})
        if breakdown:
            lines.append(f"311 complaints: {', '.join(f'{k} ({v})' for k, v in list(breakdown.items())[:4])}")
    elif category == "transit":
        buckets = enriched.get("collision_time_buckets", {})
        if buckets and sum(buckets.values()) > 0:
            peak = max(buckets, key=buckets.get)
            peak_labels = {"morning_6_12": "morning", "afternoon_12_18": "afternoon",
                           "evening_18_24": "evening", "night_0_6": "night"}
            lines.append(f"Crash timing peak: {peak_labels.get(peak, peak)} ({buckets[peak]} of {sum(buckets.values())})")
    elif category == "amenities":
        parks = enriched.get("nearest_parks", [])
        if parks:
            lines.append(f"Nearby parks: {', '.join(parks[:3])}")
        highlights = enriched.get("restaurant_highlights", {})
        if highlights.get("a_grade_count"):
            names = highlights.get("sample_a_names", [])
            extra = f" including {', '.join(names[:2])}" if names else ""
            lines.append(f"A-grade restaurants: {highlights['a_grade_count']}{extra}")
        tree_health = enriched.get("tree_health", {})
        if tree_health:
            lines.append(f"Tree health: {', '.join(f'{k}: {v}' for k, v in tree_health.items())}")
    elif category == "building":
        age = enriched.get("violation_age", {})
        if age:
            lines.append(f"Violation age: avg {age.get('avg_age_days', '?')}d, max {age.get('max_age_days', '?')}d")
        for flag in detail_items.get("building_flags", [])[:3]:
            lines.append(f"Flag: {flag.get('summary', '')} (severity: {flag.get('severity', '?')})")

    # Relevant anomalies only
    sig_map = signal_to_category_map()
    for sig, trend_data in trends.items():
        anom = trend_data.get("anomaly", {})
        if not anom.get("is_anomaly"):
            continue
        if sig_map.get(sig, "") != category and not sig.startswith(category):
            continue
        z = anom.get("z_score")
        ctx = anom.get("context", "")
        lines.append(f"ANOMALY — {sig}" + (f" (z={z:+.2f})" if z else "") + (f": {ctx}" if ctx else ""))

    # Relevant incidents only
    for inc in detail_items.get("recent_incidents", []):
        if _INCIDENT_TO_CATEGORY.get(inc.get("kind", "").lower(), "") == category:
            lines.append(f"Incident: {inc.get('summary', '')} ({inc.get('date', '')})")

    # Compact instructions
    lines.append("")
    if report_mode == "individual":
        lines.append(f"Analyze {category} in 2-3 sentences for a resident. Friendly, cite numbers, practical advice.")
    else:
        lines.append(f"Analyze {category} in 2-3 sentences for city officials. Data-driven, cite numbers.")
    lines.append("Compare to citywide baseline when available. Never show raw P25/P50/P75 or z-scores.")

    return "\n".join(lines)


def _build_synthesis_prompt(payload: dict, category_analyses: dict[str, str], report_mode: str) -> str:
    """Combine per-category analyses into a final cohesive report."""
    target = payload.get("target", {})
    radius = target.get("radius_m", 500)
    location_desc = target.get("matched_address") or target.get("borough", "NYC")
    scores = payload.get("scores", {})
    priority_order = payload.get("priority_profile", {}).get("order", [])
    patterns = payload.get("patterns", [])
    actions = payload.get("priority_actions", [])
    why_now = payload.get("why_now", [])
    data_gaps = payload.get("data_gaps", [])

    lines = [f"Location: {location_desc}, {radius}m radius"]

    # All scores
    score_parts = []
    for dim in ["overall", "safety", "transit", "amenities", "building"]:
        val = scores.get(dim)
        if val is not None:
            score_parts.append(f"{dim}={val}/100")
    if score_parts:
        lines.append(f"Scores: {', '.join(score_parts)}")
    if priority_order:
        lines.append(f"User priorities: {' > '.join(priority_order)}")

    # Top priority actions
    if actions:
        lines.extend(["", "Top priorities:"])
        for item in actions[:3]:
            lines.append(f"- {item['action']} (severity={item.get('severity')}, momentum={item.get('momentum')})")

    # Trend signals
    if why_now:
        lines.extend(["", "Trend signals:"])
        for item in why_now[:3]:
            lines.append(f"- {item['signal']}")

    # Cross-signal patterns
    if patterns:
        lines.extend(["", "Cross-signal patterns:"])
        for pat in patterns:
            lines.append(f"- {pat['title']}: {pat.get('summary', '')}")

    # Pre-digested category analyses from stage 1
    lines.extend(["", "=== CATEGORY ANALYSES (written by prior step) ==="])
    for cat in ["safety", "transit", "amenities", "building"]:
        text = category_analyses.get(cat, "").strip()
        if text:
            lines.append(f"[{cat.upper()}] {text}")
        else:
            lines.append(f"[{cat.upper()}] No data available.")

    if data_gaps:
        lines.extend(["", "Data gaps:"])
        for gap in data_gaps[:2]:
            lines.append(f"- {gap}")

    # Synthesis instructions
    lines.append("")
    top_priority = priority_order[0] if priority_order else "safety"
    if report_mode == "individual":
        lines.extend([
            f"Combine the category analyses above into one cohesive neighborhood report. Lead with {top_priority}.",
            "Friendly tone, practical advice, mention the overall score.",
        ])
    else:
        lines.extend([
            f"Combine the category analyses above into one governance brief. Lead with {top_priority}.",
            "Data-driven, resource allocation framing, mention scores.",
        ])
    lines.extend([
        "Prose paragraphs only. No bullets. No headers. Use location name, not coordinates.",
        "Weave in cross-signal patterns naturally. Mention data limitations briefly at end.",
    ])

    return "\n".join(lines)


def _strip_thinking(content: str | None) -> str:
    """Remove <think>...</think> blocks from model output."""
    if not content or "<think>" not in content:
        return content or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _call_llm(client, model_name: str, system_msg: str, user_prompt: str,
              extra_body: dict, max_tokens: int = 300) -> str:
    """Single LLM call. Returns content string, or empty string on failure."""
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        content = response.choices[0].message.content
        return _strip_thinking(content)
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return ""


def _resolve_model_name(client) -> str:
    configured = os.getenv("URBAN_DOSSIER_MODEL", DEFAULT_MODEL).strip()
    if configured and configured.lower() != "auto":
        return configured
    try:
        models = client.models.list()
        data = getattr(models, "data", None) or []
        if data:
            candidate = getattr(data[0], "id", None)
            if candidate:
                return str(candidate)
    except Exception:
        pass
    return "urban_dossier-local"


def generate_action_brief(payload: dict, use_llm: bool = True, timeout: int = 60, report_mode: str = "individual") -> tuple[str, str]:
    """Generate report using staged LLM calls: one per category, then synthesis.

    Each category gets a small, focused prompt so that even small local models
    can reason stably. The synthesis step combines pre-digested analyses into
    the final cohesive report.
    """
    if not use_llm or os.getenv("URBAN_DOSSIER_USE_LLM", "auto").lower() == "0":
        brief = fallback_brief(payload, report_mode=report_mode)
        return first_sentence(brief), brief
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
            api_key=os.getenv("OPENAI_API_KEY", DEFAULT_OPENAI_API_KEY),
            timeout=timeout,
        )
        model_name = _resolve_model_name(client)
        extra_body: dict = {"chat_template_kwargs": {"enable_thinking": False}}

        if report_mode == "individual":
            cat_system = "You are a NYC neighborhood advisor. Analyze data concisely with specific numbers."
            synth_system = "You are a NYC neighborhood advisor. Write clear, friendly analysis. Be honest about strengths and concerns."
        else:
            cat_system = "You are a NYC urban data analyst. Analyze data concisely with specific numbers."
            synth_system = "You are a NYC urban data analyst. Write data-driven governance briefs with numbers and recommendations."

        # Stage 1 — per-category analysis (small prompt, small response each)
        category_analyses: dict[str, str] = {}
        for category in ["safety", "transit", "amenities", "building"]:
            prompt = _build_category_prompt(payload, category, report_mode)
            logger.info("Stage 1 — %s prompt: %d chars", category, len(prompt))
            result = _call_llm(client, model_name, cat_system, prompt, extra_body, max_tokens=250)
            category_analyses[category] = result

        # Stage 2 — synthesis (category texts + scores + patterns → final report)
        synth_prompt = _build_synthesis_prompt(payload, category_analyses, report_mode)
        logger.info("Stage 2 — synthesis prompt: %d chars", len(synth_prompt))
        brief = _call_llm(client, model_name, synth_system, synth_prompt, extra_body, max_tokens=800)

        if not brief or len(brief.strip()) <= 20:
            logger.warning("Staged LLM produced empty/short output, falling back to template")
            brief = fallback_brief(payload, report_mode=report_mode)
        return first_sentence(brief), brief
    except Exception:
        logger.exception("Staged report generation failed, falling back to template")
        brief = fallback_brief(payload, report_mode=report_mode)
        return first_sentence(brief), brief
