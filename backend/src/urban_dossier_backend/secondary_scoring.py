"""Category scores, and how much evidence each one actually rests on.

`_weighted_score` renormalises over whichever sub-metrics happen to have a
value. That is the right aggregation -- filling absent metrics with zero would
score a data gap as a hazard -- but on its own it is an *undisclosed* listwise
deletion, and it makes two very different situations produce the same shape of
answer:

    five of five safety metrics present   -> safety 68
    one  of five safety metrics present   -> safety 70

Nothing in the payload distinguished them. The second number is a single
collision reading wearing the same clothes as a five-source composite, and it
reads as the more favourable of the two. The composite-indicator literature is
blunt about the size of this effect: dropping 10-20% of cases is enough to bias
estimates substantially when the missingness is not random, and here it very
much is not -- coverage is thinnest at the edges of the city and over water,
exactly where the map is least trustworthy already.

So this module now reports coverage beside every score. It deliberately does
*not* change any score. Disclosure and adjustment are separate steps: what a
thin score should be worth is the uncertainty question that item 1.4 answers
with a perturbation range, and folding a guess about it into the point estimate
now would make that analysis impossible to interpret. `compute_secondary_scores`
keeps its exact previous behaviour and return type; callers that want the extra
information ask for it explicitly via `compute_scores_with_coverage`.

Two ratios are reported per category because they answer different questions:

``ratio``
    Share of the category's *weight* that produced a value. Weight rather than
    count, because sub-metrics are not equally important -- losing the 0.25
    collision term is not the same as losing the 0.10 LinkNYC one.
``available`` / ``total``
    The plain count, for display.

and for overall, ``effective_ratio`` folds each category's own coverage into
the category weights, so a composite built from three categories that are each
half-covered reports 0.5 rather than 1.0.
"""
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


def _category_weights(category_id: str) -> dict[str, float]:
    return {
        name: cfg["weight"]
        for name, cfg in CATEGORY_CONFIG[category_id]["sub_datasets"].items()
    }


def _fallback_subscores(current_state: dict, baselines: dict) -> dict[str, dict[str, int | None] | None]:
    """Per-sub-metric fallback scores, before aggregation.

    Returns ``None`` for a category with no usable inputs at all, which is
    distinct from a category whose inputs were all present but scored zero.

    This used to aggregate inline and return one number per category. Keeping
    the sub-scores lets the coverage calculation treat the fallback path and
    the prepared-table path identically instead of guessing at the former.
    """
    safety = current_state.get("safety", {})
    transit = current_state.get("transit", {})
    amenities = current_state.get("amenities", {})
    building = current_state.get("building", {})

    out: dict[str, dict[str, int | None] | None] = {}

    if not _has_any_data(safety, ["collision_count_500m", "rodent_positive_500m", "sanitation_311_recent_count", "ems_avg_response_seconds", "fire_avg_response_seconds"]):
        out["safety"] = None
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
        out["safety"] = {
            "collision": collision_score,
            "rodent": rodent_score,
            "311_sanitation": sanitation_score,
            "ems_response": ems_score,
            "fire_response": fire_score,
        }

    # Transit has no fallback since v3.8.0. Its only fallback formula scored
    # the collision copy that the correlation work removed from the category,
    # and none of the four access metrics (subway, bus, bike routes, open
    # streets) can be derived from the analyse-point state. Without prepared
    # score tables, transit is honestly None -- which the old path disguised
    # as a road-safety number wearing a transit label.
    out["transit"] = None

    if not _has_any_data(amenities, ["park_acres_zip_proxy", "tree_count_500m", "toilet_count_1km", "linknyc_count_500m", "restaurant_count_500m"]):
        out["amenities"] = None
    else:
        parks_score = None
        if amenities.get("park_acres_zip_proxy") is not None:
            parks_score = _clamp(45.0 + min(amenities["park_acres_zip_proxy"] / 4.0, 25))
        trees_score = None
        if amenities.get("tree_count_500m") is not None:
            trees_score = _clamp(45.0 + min(amenities["tree_count_500m"] / 20.0, 20))
        toilets_score = None
        if amenities.get("toilet_count_1km") is not None:
            toilets_score = _clamp(40.0 + min(amenities["toilet_count_1km"] * 7, 35))
        linknyc_score = None
        if amenities.get("linknyc_count_500m") is not None:
            linknyc_score = _clamp(45.0 + min(amenities["linknyc_count_500m"] * 8, 30))
        restaurants_score = None
        if amenities.get("restaurant_count_500m") is not None:
            restaurants_score = _clamp(
                40.0
                + min(amenities["restaurant_count_500m"] / 4.0, 25)
                # The critical rate only modifies an established count, so an
                # absent rate leaves the count unadjusted rather than blocking
                # the score.
                - min(amenities.get("restaurant_critical_rate_500m", 0) * 25, 20)
            )
        out["amenities"] = {
            "parks_access": parks_score,
            "trees": trees_score,
            "public_toilets": toilets_score,
            "linknyc": linknyc_score,
            "restaurant_context": restaurants_score,
            "facilities": None,
        }

    if not _has_any_data(building, ["open_class_c_250m", "open_class_b_250m", "aep_count_250m"]):
        out["building"] = None
    else:
        # Class B and C are two severities of the same violation count, so
        # either one is enough to score the metric; neither means no reading.
        violation_score = None
        if (
            building.get("open_class_c_250m") is not None
            or building.get("open_class_b_250m") is not None
        ):
            violation_score = _clamp(
                100.0
                - min(building.get("open_class_c_250m", 0) * 12, 45)
                - min(building.get("open_class_b_250m", 0) * 6, 20)
            )
        aep_score = None
        if building.get("aep_count_250m") is not None:
            aep_score = _clamp(100.0 - min(building["aep_count_250m"] * 25, 45))
        out["building"] = {
            "housing_violations": violation_score,
            "aep": aep_score,
        }

    return out


def _fallback_scores(current_state: dict, baselines: dict) -> dict[str, int | None]:
    """Aggregated fallback scores. Preserved for callers outside this module."""
    subscores = _fallback_subscores(current_state, baselines)
    return {
        category_id: (
            None if subs is None else _weighted_score(subs, _category_weights(category_id))
        )
        for category_id, subs in subscores.items()
    }


def _coverage_for(category_id: str, subscores: dict[str, int | None] | None) -> dict:
    """How much of a category's intended evidence base is present."""
    weights = _category_weights(category_id)
    weighted = {name: w for name, w in weights.items() if w > 0}
    weight_total = sum(weighted.values())
    subscores = subscores or {}
    present = [
        name
        for name in weighted
        if subscores.get(name) is not None
    ]
    weight_available = sum(weighted[name] for name in present)
    return {
        "available": len(present),
        "total": len(weighted),
        "present": sorted(present),
        "missing": sorted(name for name in weighted if name not in present),
        "weight_available": round(weight_available, 4),
        "weight_total": round(weight_total, 4),
        "ratio": round(weight_available / weight_total, 4) if weight_total else 0.0,
    }


def compute_scores_with_coverage(
    current_state: dict,
    baselines: dict,
    prepared_scores: dict | None = None,
    user_priority_weights: dict[str, float] | None = None,
) -> tuple[dict[str, int | None], dict]:
    """Category scores plus the evidence behind each of them.

    The scores are computed exactly as they always were. The second return
    value is new and purely descriptive.
    """
    prepared_scores = prepared_scores or {}
    fallback_subscores = _fallback_subscores(current_state, baselines)

    scores: dict[str, int | None] = {}
    coverage: dict[str, dict] = {}

    for category_id, config in CATEGORY_CONFIG.items():
        weights = {name: cfg["weight"] for name, cfg in config["sub_datasets"].items()}
        ready_subscores = prepared_scores.get(category_id) or {}
        if ready_subscores:
            used = {name: ready_subscores.get(name) for name in config["sub_datasets"]}
            source = "prepared"
        else:
            used = fallback_subscores.get(category_id)
            source = "fallback" if used else "none"
            if used is None:
                used = {}
        scores[category_id] = _weighted_score(used, weights) if used else None
        entry = _coverage_for(category_id, used)
        entry["source"] = source
        coverage[category_id] = entry

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

    # Overall coverage is measured against every category that *could* have
    # contributed, not just the ones that did -- otherwise a composite built
    # from one surviving category reports itself fully covered.
    if user_priority_weights:
        candidate_weights = {k: v for k, v in user_priority_weights.items() if v > 0}
    else:
        candidate_weights = {
            category_id: cfg.get("weight_in_overall", 0.0)
            for category_id, cfg in CATEGORY_CONFIG.items()
            if cfg.get("weight_in_overall", 0.0) > 0
        }
    candidate_total = sum(candidate_weights.values())
    contributed = sorted(k for k in candidate_weights if scores.get(k) is not None)
    weight_contributed = sum(candidate_weights[k] for k in contributed)
    # Fold each contributing category's own coverage into its weight, so a
    # score assembled from thin categories cannot claim full evidence.
    effective = sum(
        candidate_weights[k] * coverage[k]["ratio"] for k in contributed
    )
    coverage["overall"] = {
        "categories_used": contributed,
        "categories_missing": sorted(k for k in candidate_weights if k not in contributed),
        "weight_available": round(weight_contributed, 4),
        "weight_total": round(candidate_total, 4),
        "ratio": round(weight_contributed / candidate_total, 4) if candidate_total else 0.0,
        "effective_ratio": round(effective / candidate_total, 4) if candidate_total else 0.0,
        "weakest_category_ratio": (
            round(min(coverage[k]["ratio"] for k in contributed), 4) if contributed else 0.0
        ),
    }
    return scores, coverage


def compute_secondary_scores(
    current_state: dict,
    baselines: dict,
    prepared_scores: dict | None = None,
    user_priority_weights: dict[str, float] | None = None,
) -> dict[str, int | None]:
    """Category scores only. Unchanged behaviour; kept for existing callers."""
    scores, _ = compute_scores_with_coverage(
        current_state, baselines, prepared_scores, user_priority_weights
    )
    return scores
