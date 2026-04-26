from __future__ import annotations

from .categories import CATEGORY_CONFIG


def _clamp(value: float) -> int:
    return int(max(0, min(100, round(value))))


def _has_any_data(module: dict, keys: list[str]) -> bool:
    return any(module.get(key) is not None for key in keys)


def _weighted_score(subscores: dict[str, int | None], weights: dict[str, float]) -> int | None:
    available = {name: value for name, value in subscores.items() if value is not None and weights.get(name, 0) > 0}
    if not available:
        return None
    total_weight = sum(weights[name] for name in available)
    if total_weight <= 0:
        return None
    return _clamp(sum(available[name] * weights[name] for name in available) / total_weight)


def _fallback_scores(current_state: dict, baselines: dict) -> dict[str, int | None]:
    safety = current_state.get("safety", {})
    transit = current_state.get("transit", {})
    amenities = current_state.get("amenities", {})
    building = current_state.get("building", {})

    if not _has_any_data(safety, ["collision_count_500m", "rodent_positive_500m", "sanitation_311_recent_count", "ems_avg_response_seconds", "fire_avg_response_seconds"]):
        safety_score_val = None
    else:
        collision_score = None
        if safety.get("collision_count_500m") is not None:
            collision_score = _clamp(
                100.0
                - min((safety.get("collision_count_500m", 0) / max(baselines["collision"].get("p75", 1), 1)) * 50, 60)
                - min(safety.get("ped_cyclist_injuries_1km", 0) * 2, 15)
            )
        rodent_score = None
        if safety.get("rodent_positive_500m") is not None:
            rodent_score = _clamp(100.0 - min((safety["rodent_positive_500m"] / max(baselines["rodent"].get("p75", 1), 1)) * 60, 60))
        sanitation_score = None
        if safety.get("sanitation_311_recent_count") is not None:
            sanitation_score = _clamp(100.0 - min(safety["sanitation_311_recent_count"] * 2.5, 55))
        ems_score = None
        fire_score = None
        if safety.get("ems_avg_response_seconds") is not None:
            ems_score = _clamp(100.0 - min((safety["ems_avg_response_seconds"] / max(baselines["ems_response"].get("p75", 1), 1)) * 45, 55))
        if safety.get("fire_avg_response_seconds") is not None:
            fire_score = _clamp(100.0 - min((safety["fire_avg_response_seconds"] / max(baselines["fire_response"].get("p75", 1), 1)) * 35, 45))
        safety_score_val = _weighted_score(
            {
                "collision": collision_score,
                "rodent": rodent_score,
                "311_sanitation": sanitation_score,
                "ems_response": ems_score,
                "fire_response": fire_score,
            },
            {name: cfg["weight"] for name, cfg in CATEGORY_CONFIG["safety"]["sub_datasets"].items()},
        )

    if not _has_any_data(transit, ["collision_count_500m", "ped_cyclist_injuries_1km"]):
        transit_score_val = None
    else:
        collision_transport = _clamp(
            100.0
            - min((transit.get("collision_count_500m", 0) / max(baselines["collision"].get("p75", 1), 1)) * 55, 65)
            - min(transit.get("ped_cyclist_injuries_1km", 0) * 2, 20)
        )
        transit_score_val = _weighted_score(
            {"collision_transport": collision_transport, "subway": None, "bus": None},
            {name: cfg["weight"] for name, cfg in CATEGORY_CONFIG["transit"]["sub_datasets"].items()},
        )

    if not _has_any_data(amenities, ["park_acres_zip_proxy", "tree_count_500m", "toilet_count_1km", "linknyc_count_500m", "restaurant_count_500m"]):
        amenities_score_val = None
    else:
        parks_score = _clamp(45.0 + min(amenities.get("park_acres_zip_proxy", 0) / 4.0, 25))
        trees_score = _clamp(45.0 + min(amenities.get("tree_count_500m", 0) / 20.0, 20))
        toilets_score = _clamp(40.0 + min(amenities.get("toilet_count_1km", 0) * 7, 35))
        linknyc_score = _clamp(45.0 + min(amenities.get("linknyc_count_500m", 0) * 8, 30))
        restaurants_score = _clamp(
            40.0
            + min(amenities.get("restaurant_count_500m", 0) / 4.0, 25)
            - min(amenities.get("restaurant_critical_rate_500m", 0) * 25, 20)
        )
        amenities_score_val = _weighted_score(
            {
                "parks_access": parks_score,
                "trees": trees_score,
                "public_toilets": toilets_score,
                "linknyc": linknyc_score,
                "restaurant_context": restaurants_score,
                "facilities": None,
            },
            {name: cfg["weight"] for name, cfg in CATEGORY_CONFIG["amenities"]["sub_datasets"].items()},
        )

    if not _has_any_data(building, ["open_class_c_250m", "open_class_b_250m", "aep_count_250m"]):
        building_score_val = None
    else:
        violation_score = _clamp(
            100.0
            - min(building.get("open_class_c_250m", 0) * 12, 45)
            - min(building.get("open_class_b_250m", 0) * 6, 20)
        )
        aep_score = _clamp(100.0 - min(building.get("aep_count_250m", 0) * 25, 45))
        building_score_val = _weighted_score(
            {
                "housing_violations": violation_score,
                "aep": aep_score,
            },
            {name: cfg["weight"] for name, cfg in CATEGORY_CONFIG["building"]["sub_datasets"].items()},
        )

    return {
        "safety": safety_score_val,
        "transit": transit_score_val,
        "amenities": amenities_score_val,
        "building": building_score_val,
    }


def compute_secondary_scores(
    current_state: dict,
    baselines: dict,
    prepared_scores: dict | None = None,
    user_priority_weights: dict[str, float] | None = None,
) -> dict[str, int | None]:
    prepared_scores = prepared_scores or {}
    fallback_scores = _fallback_scores(current_state, baselines)

    scores: dict[str, int | None] = {}
    for category_id, config in CATEGORY_CONFIG.items():
        ready_subscores = prepared_scores.get(category_id) or {}
        if ready_subscores:
            scores[category_id] = _weighted_score(
                {name: ready_subscores.get(name) for name in config["sub_datasets"]},
                {name: cfg["weight"] for name, cfg in config["sub_datasets"].items()},
            )
        else:
            scores[category_id] = fallback_scores.get(category_id)

    # Use user priority weights for overall if provided, otherwise use fixed config weights
    if user_priority_weights:
        overall_weights = {k: v for k, v in user_priority_weights.items() if scores.get(k) is not None}
    else:
        overall_weights = {
            category_id: cfg.get("weight_in_overall", 0.0)
            for category_id, cfg in CATEGORY_CONFIG.items()
            if cfg.get("weight_in_overall", 0.0) > 0
        }
    available = {key: value for key, value in scores.items() if value is not None and key in overall_weights}
    if not available:
        scores["overall"] = None
    else:
        total_weight = sum(overall_weights[key] for key in available)
        if total_weight == 0:
            scores["overall"] = None
        else:
            overall = sum(value * overall_weights[key] for key, value in available.items()) / total_weight
            scores["overall"] = _clamp(overall)
    return scores
