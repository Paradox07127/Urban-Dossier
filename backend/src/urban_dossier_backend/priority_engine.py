from __future__ import annotations


PRIORITY_SIGNALS = {
    "rodent": {
        "action_template": "Allocate additional pest control resources around the selected area",
        "signal_template": "Rodent-positive inspections remain elevated near the selected point",
        "evidence_ids": ["rodent_500m", "trend_rodent"],
        "actionability": 0.80,
        "current_module": "safety",
        "current_key": "rodent_positive_500m",
        "baseline_key": "rodent",
    },
    "311_sanitation": {
        "action_template": "Increase sanitation inspection and cleanup follow-up near the selected area",
        "signal_template": "Sanitation-related 311 complaints are elevated near the selected point",
        "evidence_ids": ["311_sanitation_500m", "trend_311_sanitation"],
        "actionability": 0.85,
        "current_module": "safety",
        "current_key": "sanitation_311_recent_count",
        "baseline_key": "rodent",
    },
    "collision": {
        "action_template": "Review traffic safety measures in the nearby corridor",
        "signal_template": "Recent collision activity is elevated around the selected point",
        "evidence_ids": ["collisions_500m", "trend_collision"],
        "actionability": 0.75,
        "current_module": "transit",
        "current_key": "collision_count_500m",
        "baseline_key": "collision",
    },
    "ems_response": {
        "action_template": "Review EMS coverage and dispatch pressure for the surrounding ZIP",
        "signal_template": "EMS response is elevated for the local ZIP context",
        "evidence_ids": ["ems_response", "trend_ems_response"],
        "actionability": 0.70,
        "current_module": "safety",
        "current_key": "ems_avg_response_seconds",
        "baseline_key": "ems_response",
    },
    "fire_response": {
        "action_template": "Review fire dispatch response conditions for the surrounding ZIP",
        "signal_template": "Fire response is elevated for the local ZIP context",
        "evidence_ids": ["fire_response", "trend_fire_response"],
        "actionability": 0.60,
        "current_module": "safety",
        "current_key": "fire_avg_response_seconds",
        "baseline_key": "fire_response",
    },
    "housing_violations": {
        "action_template": "Prioritize housing inspection for buildings near the selected point",
        "signal_template": "Open serious housing violations are present near the selected point",
        "evidence_ids": ["housing_violations_250m", "trend_housing_violations"],
        "actionability": 0.65,
        "current_module": "building",
        "current_key": "open_class_c_250m",
        "baseline_key": "housing_violations",
    },
}


def compute_severity(current_state: dict, signal_name: str, baselines: dict) -> float:
    config = PRIORITY_SIGNALS.get(signal_name)
    if not config:
        return 0.5
    value = current_state.get(config["current_module"], {}).get(config["current_key"], 0) or 0
    p75 = baselines.get(config["baseline_key"], {}).get("p75") or 1
    return min(float(value) / float(p75), 1.0)


def compute_momentum(trend: dict, severity_hint: float = 0.0) -> float:
    rd = trend.get("recent_delta", {}).get("change_pct")
    sd = trend.get("seasonal_delta", {}).get("change_pct")
    bg = trend.get("baseline_gap", {}).get("gap_pct")
    persistence = trend.get("persistence", {}).get("consecutive_above", 0)
    score = 0.0
    if rd is not None and rd > 0:
        score += min(rd / 50.0, 0.4)
    if sd is not None and sd > 0:
        score += min(sd / 50.0, 0.3)
    if bg is not None and bg > 0:
        score += min(bg / 100.0, 0.2)
    score += min(persistence * 0.1, 0.3)
    if score == 0.0 and trend.get("direction") == "elevated":
        score = 0.15
    # If severity is high but momentum formula yielded 0 (e.g. trend data is sparse
    # or the signal is stable at a bad level), give a floor so the issue still surfaces.
    # A location with 76 open Class C violations or 602s EMS response should not be
    # invisible just because the trend is flat.
    if score == 0.0 and severity_hint >= 0.15:
        score = 0.12
    return min(score, 1.0)


def compute_confidence(current_state: dict, trend: dict, signal_name: str) -> float:
    config = PRIORITY_SIGNALS.get(signal_name)
    if not config:
        return 0.5
    has_current = current_state.get(config["current_module"], {}).get(config["current_key"]) is not None
    has_trend = trend.get("direction") != "insufficient_data"
    has_baseline = trend.get("baseline_gap", {}).get("gap_pct") is not None
    return (0.4 if has_current else 0) + (0.35 if has_trend else 0) + (0.25 if has_baseline else 0)


def compute_priority_actions(current_state: dict, trends: dict, baselines: dict, priority_weights: dict, signal_to_category: dict) -> list[dict]:
    candidates: list[dict] = []
    for signal_name, trend in trends.items():
        config = PRIORITY_SIGNALS.get(signal_name)
        if not config:
            continue
        category = signal_to_category.get(signal_name, "safety")
        raw_preference = priority_weights.get(category, 0.25)
        # Amplify preference impact: rank 1 gets 3x boost, rank 2 gets 1.5x, rank 3 gets 1x
        # This ensures user priority order visibly changes the ranking
        preference_boost = 1.0 + (raw_preference ** 0.5) * 2.0  # ~3.0 for rank1, ~1.7 for rank2, ~1.4 for rank3
        severity = compute_severity(current_state, signal_name, baselines)
        momentum = compute_momentum(trend, severity_hint=severity)
        confidence = compute_confidence(current_state, trend, signal_name)
        priority_score = severity * momentum * confidence * config["actionability"] * preference_boost
        if priority_score <= 0.005:
            continue
        candidates.append({
            "signal": signal_name,
            "category": category,
            "action": config["action_template"],
            "signal_description": config["signal_template"],
            "severity": round(severity, 2),
            "momentum": round(momentum, 2),
            "confidence": round(confidence, 2),
            "actionability": round(config["actionability"], 2),
            "preference_weight": round(raw_preference, 2),
            "preference_boost": round(preference_boost, 2),
            "priority_score": round(priority_score, 3),
            "evidence_ids": list(config["evidence_ids"]),
        })
    candidates.sort(key=lambda item: item["priority_score"], reverse=True)
    for idx, item in enumerate(candidates, start=1):
        item["rank"] = idx
    return candidates[:5]
