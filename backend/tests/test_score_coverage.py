"""Coverage is reported, and reporting it moves no score.

Two jobs here.

The first is a regression guard. Splitting the fallback path into per-metric
sub-scores touched the code that produces every category number in the product,
so `GOLDEN` pins the output of `compute_secondary_scores` on eight states --
empty, complete, single-signal, prepared, partial-prepared, priority-weighted
and saturated -- captured from the implementation *before* the split. If a
refactor here shifts a score by one point, these fail.

The second is the actual feature. The case that motivates it is in the golden
set twice: `prepared_full` scores safety 68 from five sub-metrics, and
`prepared_partial_safety` scores safety 70 from one. The higher number rests on
a fifth of the evidence, and before this change the payload said nothing about
that.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.secondary_scoring import (
    compute_scores_with_coverage,
    compute_secondary_scores,
)


BASELINES = {
    "collision": {"p75": 40},
    "rodent": {"p75": 12},
    "ems_response": {"p75": 500},
    "fire_response": {"p75": 300},
}

SAFETY_KEYS = {
    "collision_count_500m", "ped_cyclist_injuries_1km", "rodent_positive_500m",
    "sanitation_311_recent_count", "ems_avg_response_seconds", "fire_avg_response_seconds",
}
TRANSIT_KEYS = {"collision_count_500m", "ped_cyclist_injuries_1km"}
AMENITY_KEYS = {
    "park_acres_zip_proxy", "tree_count_500m", "toilet_count_1km",
    "linknyc_count_500m", "restaurant_count_500m", "restaurant_critical_rate_500m",
}
BUILDING_KEYS = {"open_class_c_250m", "open_class_b_250m", "aep_count_250m"}


def state(**kw):
    return {
        "safety": {k: v for k, v in kw.items() if k in SAFETY_KEYS},
        "transit": {k: v for k, v in kw.items() if k in TRANSIT_KEYS},
        "amenities": {k: v for k, v in kw.items() if k in AMENITY_KEYS},
        "building": {k: v for k, v in kw.items() if k in BUILDING_KEYS},
    }


FULL = dict(
    collision_count_500m=30, ped_cyclist_injuries_1km=3, rodent_positive_500m=8,
    sanitation_311_recent_count=10, ems_avg_response_seconds=420,
    fire_avg_response_seconds=260, park_acres_zip_proxy=40, tree_count_500m=300,
    toilet_count_1km=2, linknyc_count_500m=3, restaurant_count_500m=60,
    restaurant_critical_rate_500m=0.2, open_class_c_250m=1, open_class_b_250m=4,
    aep_count_250m=0,
)

PREPARED_FULL = {
    "safety": {"collision": 70, "rodent": 60, "311_sanitation": 55, "ems_response": 80, "fire_response": 75},
    "transit": {"collision_transport": 65, "subway": 90, "bus": 70, "bike_routes": 50, "open_streets": 20},
    "amenities": {"parks_access": 60, "trees": 55, "public_toilets": 40, "linknyc": 30,
                  "restaurant_context": 75, "facilities": 50},
    "building": {"housing_violations": 65, "aep": 90},
}

PREPARED_PARTIAL = {
    "safety": {"collision": 70, "rodent": None, "311_sanitation": None,
               "ems_response": None, "fire_response": None}
}

# name -> (state, prepared, priority, expected scores)
GOLDEN = {
    "empty": (
        state(), None, None,
        {"safety": None, "transit": None, "amenities": None, "building": None, "overall": None},
    ),
    "full_fallback": (
        state(**FULL), None, None,
        {"safety": 64, "transit": 53, "amenities": 56, "building": 78, "overall": 58},
    ),
    "safety_one_signal": (
        state(collision_count_500m=30), None, None,
        {"safety": 62, "transit": 59, "amenities": None, "building": None, "overall": 61},
    ),
    "rodent_only": (
        state(rodent_positive_500m=25), None, None,
        {"safety": 40, "transit": None, "amenities": None, "building": None, "overall": 40},
    ),
    "prepared_full": (
        state(**FULL), PREPARED_FULL, None,
        {"safety": 68, "transit": 66, "amenities": 55, "building": 72, "overall": 64},
    ),
    "prepared_partial_safety": (
        state(**FULL), PREPARED_PARTIAL, None,
        {"safety": 70, "transit": 53, "amenities": 56, "building": 78, "overall": 61},
    ),
    "priority_weights": (
        state(**FULL), PREPARED_FULL, {"safety": 0.5, "transit": 0.3, "amenities": 0.2},
        {"safety": 68, "transit": 66, "amenities": 55, "building": 72, "overall": 65},
    ),
    "extreme_bad": (
        state(collision_count_500m=9999, rodent_positive_500m=9999,
              sanitation_311_recent_count=9999, ems_avg_response_seconds=9999,
              fire_avg_response_seconds=9999), None, None,
        {"safety": 44, "transit": 35, "amenities": None, "building": None, "overall": 40},
    ),
}


# One live input per category, which is what exposed the fabrication bug: the
# eight cases above never hit it, because they either supply every input or
# supply only safety inputs, and safety was the one category already guarded.
#
# Values here are post-fix. The pre-fix numbers are recorded in
# `FABRICATED_BEFORE` below rather than deleted, so the size of the correction
# stays in the record.
SINGLE_SIGNAL = {
    "only_sanitation": (
        state(sanitation_311_recent_count=10),
        {"safety": 75, "transit": None, "amenities": None, "building": None, "overall": 75},
    ),
    "only_ems": (
        state(ems_avg_response_seconds=420),
        {"safety": 62, "transit": None, "amenities": None, "building": None, "overall": 62},
    ),
    "only_fire": (
        state(fire_avg_response_seconds=260),
        {"safety": 70, "transit": None, "amenities": None, "building": None, "overall": 70},
    ),
    "only_parks": (
        state(park_acres_zip_proxy=40),
        {"safety": None, "transit": None, "amenities": 55, "building": None, "overall": 55},
    ),
    "only_trees": (
        state(tree_count_500m=300),
        {"safety": None, "transit": None, "amenities": 60, "building": None, "overall": 60},
    ),
    "only_toilets": (
        state(toilet_count_1km=2),
        {"safety": None, "transit": None, "amenities": 54, "building": None, "overall": 54},
    ),
    "only_linknyc": (
        state(linknyc_count_500m=3),
        {"safety": None, "transit": None, "amenities": 69, "building": None, "overall": 69},
    ),
    "only_restaurants": (
        state(restaurant_count_500m=60, restaurant_critical_rate_500m=0.2),
        {"safety": None, "transit": None, "amenities": 50, "building": None, "overall": 50},
    ),
    "only_violations": (
        state(open_class_c_250m=1, open_class_b_250m=4),
        {"safety": None, "transit": None, "amenities": None, "building": 68, "overall": None},
    ),
    "only_aep": (
        state(aep_count_250m=1),
        {"safety": None, "transit": None, "amenities": None, "building": 75, "overall": None},
    ),
    "only_ped_injuries": (
        # collision_count is a real zero here, not a gap, so the metric scores.
        state(collision_count_500m=0, ped_cyclist_injuries_1km=6),
        {"safety": 88, "transit": 88, "amenities": None, "building": None, "overall": 88},
    ),
}

# What each of these produced before absent inputs stopped being read as zero.
# category -> (before, after). Kept as documentation of a deliberate change.
FABRICATED_BEFORE = {
    "only_parks": ("amenities", 46, 55),
    "only_trees": ("amenities", 46, 60),
    "only_toilets": ("amenities", 45, 54),
    "only_linknyc": ("amenities", 46, 69),
    "only_restaurants": ("amenities", 45, 50),
    "only_violations": ("building", 78, 68),
    "only_aep": ("building", 92, 75),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_scores_are_unchanged_by_the_coverage_work(name):
    st, prepared, priority, expected = GOLDEN[name]
    assert compute_secondary_scores(st, BASELINES, prepared, user_priority_weights=priority) == expected


@pytest.mark.parametrize("name", sorted(SINGLE_SIGNAL))
def test_single_signal_scores(name):
    st, expected = SINGLE_SIGNAL[name]
    assert compute_secondary_scores(st, BASELINES, None) == expected


# --- absent inputs must not be read as zero ---------------------------------


@pytest.mark.parametrize("name", sorted(FABRICATED_BEFORE))
def test_the_fabrication_correction_landed(name):
    """These scores moved on purpose, and by this much.

    Before, a missing input fell through `.get(key, 0)` into the formula. A
    location with no housing-violation data scored 100 on housing violations --
    a perfect result manufactured from nothing, carrying 70% of the building
    category. Amenities was worse: any single live input produced five
    sub-scores, four of them baselines invented from absent data.
    """
    category, before, after = FABRICATED_BEFORE[name]
    st, _ = SINGLE_SIGNAL[name]
    got = compute_secondary_scores(st, BASELINES, None)
    assert got[category] == after
    assert got[category] != before


def test_one_live_input_produces_exactly_one_sub_score():
    """The general form of the bug, not just the seven recorded instances."""
    probes = [
        ("safety", "collision_count_500m", 30),
        ("safety", "rodent_positive_500m", 8),
        ("safety", "sanitation_311_recent_count", 10),
        ("safety", "ems_avg_response_seconds", 420),
        ("safety", "fire_avg_response_seconds", 260),
        ("transit", "collision_count_500m", 30),
        ("amenities", "park_acres_zip_proxy", 40),
        ("amenities", "tree_count_500m", 300),
        ("amenities", "toilet_count_1km", 2),
        ("amenities", "linknyc_count_500m", 3),
        ("amenities", "restaurant_count_500m", 60),
        ("building", "open_class_c_250m", 1),
        ("building", "aep_count_250m", 1),
    ]
    for category, key, value in probes:
        st = {"safety": {}, "transit": {}, "amenities": {}, "building": {}}
        st[category][key] = value
        _, cov = compute_scores_with_coverage(st, BASELINES, None)
        assert cov[category]["available"] == 1, (
            f"{category} with only {key} produced {cov[category]['available']} "
            f"sub-scores: {cov[category]['present']}"
        )


def test_missing_housing_violation_data_no_longer_scores_a_perfect_hundred():
    """The single worst instance, pinned on its own."""
    st = {"safety": {}, "transit": {}, "amenities": {}, "building": {"aep_count_250m": 1}}
    _, cov = compute_scores_with_coverage(st, BASELINES, None)
    assert cov["building"]["present"] == ["aep"]
    assert "housing_violations" in cov["building"]["missing"]
    assert cov["building"]["ratio"] == pytest.approx(0.30)


def test_an_absent_primary_input_blocks_the_metric_even_with_a_live_modifier():
    """Pedestrian injuries modify the collision term; they cannot stand in for it."""
    st = {
        "safety": {"ped_cyclist_injuries_1km": 6},
        "transit": {"ped_cyclist_injuries_1km": 6},
        "amenities": {},
        "building": {},
    }
    scores, cov = compute_scores_with_coverage(st, BASELINES, None)
    assert scores["transit"] is None
    assert cov["transit"]["available"] == 0


def test_a_real_zero_still_scores():
    """Guarding on None must not swallow a genuine count of zero."""
    st = {
        "safety": {"collision_count_500m": 0},
        "transit": {"collision_count_500m": 0},
        "amenities": {},
        "building": {},
    }
    scores, cov = compute_scores_with_coverage(st, BASELINES, None)
    assert scores["safety"] == 100
    assert cov["safety"]["present"] == ["collision"]


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_the_two_entry_points_agree(name):
    """`compute_secondary_scores` must stay a pure projection of the new call."""
    st, prepared, priority, _ = GOLDEN[name]
    plain = compute_secondary_scores(st, BASELINES, prepared, user_priority_weights=priority)
    withcov, _ = compute_scores_with_coverage(st, BASELINES, prepared, user_priority_weights=priority)
    assert plain == withcov


def _coverage(name):
    st, prepared, priority, _ = GOLDEN[name]
    _, cov = compute_scores_with_coverage(st, BASELINES, prepared, user_priority_weights=priority)
    return cov


# --- the case the feature exists for ---------------------------------------


def test_a_one_source_score_and_a_five_source_score_are_now_distinguishable():
    """The defect, stated as a test.

    safety 70 on one metric used to be indistinguishable from safety 68 on
    five. The scores still differ by two points; the coverage differs by four
    fifths.
    """
    thin = _coverage("prepared_partial_safety")["safety"]
    thick = _coverage("prepared_full")["safety"]

    assert thin["available"] == 1
    assert thick["available"] == 5
    assert thin["ratio"] == pytest.approx(0.25)
    assert thick["ratio"] == pytest.approx(1.0)


def test_missing_sub_metrics_are_named_not_just_counted():
    thin = _coverage("prepared_partial_safety")["safety"]
    assert thin["present"] == ["collision"]
    assert thin["missing"] == ["311_sanitation", "ems_response", "fire_response", "rodent"]


def test_coverage_is_weighted_not_counted():
    """Losing the 0.25 collision term is not the same as losing 0.10 LinkNYC.

    One of five metrics present gives ratio 0.25, not 0.20, because collision
    carries a quarter of the category.
    """
    thin = _coverage("prepared_partial_safety")["safety"]
    assert thin["available"] / thin["total"] == pytest.approx(0.2)
    assert thin["ratio"] == pytest.approx(0.25)


def test_a_rodent_only_reading_reports_almost_no_evidence():
    """overall 40 looks decisive; it rests on 8% of the intended evidence."""
    cov = _coverage("rodent_only")
    assert cov["overall"]["effective_ratio"] == pytest.approx(0.08)
    assert cov["overall"]["categories_used"] == ["safety"]
    assert cov["overall"]["categories_missing"] == ["amenities", "transit"]


def test_effective_ratio_folds_category_coverage_into_category_weight():
    """Three half-covered categories must not report themselves fully covered."""
    cov = _coverage("full_fallback")["overall"]
    assert cov["ratio"] == pytest.approx(1.0)  # all three categories produced a score
    assert cov["effective_ratio"] < cov["ratio"]  # but not from all their metrics
    assert cov["effective_ratio"] == pytest.approx(0.745)


def test_the_transit_fallback_declares_itself_thin():
    """A fallback transit score is one collision reading; coverage says 0.30."""
    cov = _coverage("full_fallback")["transit"]
    assert cov["source"] == "fallback"
    assert cov["present"] == ["collision_transport"]
    assert cov["ratio"] == pytest.approx(0.30)


def test_source_distinguishes_prepared_tables_from_fallback_formulas():
    assert _coverage("prepared_full")["safety"]["source"] == "prepared"
    assert _coverage("full_fallback")["safety"]["source"] == "fallback"
    assert _coverage("empty")["safety"]["source"] == "none"


def test_no_data_reports_zero_coverage_rather_than_absent_coverage():
    """A gap must be a reported zero, not a missing key."""
    cov = _coverage("empty")
    for category in ("safety", "transit", "amenities", "building"):
        assert cov[category]["ratio"] == 0.0
        assert cov[category]["available"] == 0
        assert cov[category]["present"] == []
    assert cov["overall"]["effective_ratio"] == 0.0


def test_every_category_reports_coverage_even_when_it_scores_nothing():
    for name in GOLDEN:
        cov = _coverage(name)
        assert set(cov) == {"safety", "transit", "amenities", "building", "overall"}


def test_building_is_excluded_from_overall_coverage_because_its_weight_is_zero():
    """Building scores but never contributes, so it must not inflate coverage."""
    cov = _coverage("full_fallback")
    assert cov["building"]["ratio"] == pytest.approx(1.0)
    assert "building" not in cov["overall"]["categories_used"]
    assert "building" not in cov["overall"]["categories_missing"]


def test_weakest_category_ratio_surfaces_the_thinnest_contributor():
    cov = _coverage("full_fallback")["overall"]
    assert cov["weakest_category_ratio"] == pytest.approx(0.30)  # transit


def test_priority_weights_reweight_overall_coverage_too():
    """Coverage must be measured against the weights actually used."""
    cov = _coverage("priority_weights")["overall"]
    assert cov["weight_total"] == pytest.approx(1.0)
    assert cov["categories_used"] == ["amenities", "safety", "transit"]
    assert cov["effective_ratio"] == pytest.approx(1.0)


def test_coverage_ratios_stay_within_zero_and_one():
    for name in GOLDEN:
        cov = _coverage(name)
        for key, entry in cov.items():
            assert 0.0 <= entry["ratio"] <= 1.0, (name, key)
        assert 0.0 <= cov["overall"]["effective_ratio"] <= 1.0, name
