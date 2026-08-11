"""The registry must describe the scoring system that actually runs.

The central test here is `test_derived_config_matches_the_legacy_literal`. When
`CATEGORY_CONFIG` stopped being a hand-written dict and started being generated
from `metrics.py`, the risk was not that generation would fail loudly -- it was
that it would succeed with a weight off by 0.05 and quietly reweight every
score in the city. `LEGACY_CATEGORY_CONFIG` below is a verbatim copy of the
literal as it stood before the change, kept frozen so that risk stays covered.

Do not "fix" the frozen copy to match a new intent. If a weight should change,
change it in `metrics.py` and update this copy in the same commit, so the diff
shows the numbers moving.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.categories import CATEGORY_CONFIG, signal_to_category_map
from urban_dossier_backend.metrics import (
    CATEGORIES,
    CATEGORIES_BY_ID,
    METHODOLOGY_VERSION,
    METRICS,
    METRICS_BY_ID,
    Direction,
    duplicated_sources,
    metrics_for_category,
    overall_contribution,
    overlapping_pairs,
    registry_to_dict,
)


# Verbatim copy of `CATEGORY_CONFIG` before it was derived from the registry.
LEGACY_CATEGORY_CONFIG = {
    "safety": {
        "label": "Safety",
        "map_driving": True,
        "detail_rankable": True,
        "signals": ["collision", "rodent", "311_sanitation", "ems_response", "fire_response"],
        "weight_in_overall": 0.40,
        "sub_datasets": {
            "collision": {
                "weight": 0.25,
                "query_by": "h3",
                "score_table": "safety/collisions_scores_h3.parquet",
                "indexed_table": "safety/collisions_indexed.parquet",
            },
            "rodent": {
                "weight": 0.20,
                "query_by": "h3",
                "score_table": "safety/rodent_scores_h3.parquet",
                "indexed_table": "safety/rodent_indexed.parquet",
            },
            "311_sanitation": {
                "weight": 0.20,
                "query_by": "h3",
                "score_table": "safety/311_scores_h3.parquet",
                "indexed_table": "safety/311_safety_indexed.parquet",
            },
            "ems_response": {
                "weight": 0.20,
                "query_by": "zip",
                "score_table": "safety/ems_scores_zip.parquet",
            },
            "fire_response": {
                "weight": 0.15,
                "query_by": "zip",
                "score_table": "safety/fire_scores_zip.parquet",
            },
        },
    },
    "transit": {
        "label": "Transit",
        "map_driving": True,
        "detail_rankable": True,
        "signals": ["collision_transport", "subway", "bus", "bike_routes", "open_streets"],
        "weight_in_overall": 0.30,
        "sub_datasets": {
            "collision_transport": {
                "weight": 0.30,
                "query_by": "h3",
                "score_table": "transit/collision_transport_scores_h3.parquet",
                "indexed_table": "transit/collision_transport_indexed.parquet",
            },
            "subway": {
                "weight": 0.25,
                "query_by": "h3",
                "score_table": "transit/subway_scores_h3.parquet",
                "indexed_table": "transit/subway_indexed.parquet",
            },
            "bus": {
                "weight": 0.20,
                "query_by": "h3",
                "score_table": "transit/bus_scores_h3.parquet",
                "indexed_table": "transit/bus_indexed.parquet",
            },
            "bike_routes": {
                "weight": 0.15,
                "query_by": "h3",
                "score_table": "transit/bike_routes_scores_h3.parquet",
                "indexed_table": "transit/bike_routes_indexed.parquet",
            },
            "open_streets": {
                "weight": 0.10,
                "query_by": "h3",
                "score_table": "transit/open_streets_scores_h3.parquet",
                "indexed_table": "transit/open_streets_indexed.parquet",
            },
        },
    },
    "amenities": {
        "label": "Amenities",
        "map_driving": True,
        "detail_rankable": True,
        "signals": ["parks_access", "trees", "public_toilets", "linknyc", "restaurant_context", "facilities"],
        "weight_in_overall": 0.30,
        "sub_datasets": {
            "parks_access": {
                "weight": 0.25,
                "query_by": "zip",
                "score_table": "amenities/parks_scores_zip.parquet",
            },
            "trees": {
                "weight": 0.15,
                "query_by": "h3",
                "score_table": "amenities/trees_scores_h3.parquet",
                "indexed_table": "amenities/trees_indexed.parquet",
            },
            "public_toilets": {
                "weight": 0.15,
                "query_by": "h3",
                "score_table": "amenities/toilets_scores_h3.parquet",
                "indexed_table": "amenities/toilets_indexed.parquet",
            },
            "linknyc": {
                "weight": 0.10,
                "query_by": "h3",
                "score_table": "amenities/linknyc_scores_h3.parquet",
                "indexed_table": "amenities/linknyc_indexed.parquet",
            },
            "restaurant_context": {
                "weight": 0.20,
                "query_by": "h3",
                "score_table": "amenities/restaurants_scores_h3.parquet",
                "indexed_table": "amenities/restaurants_indexed.parquet",
            },
            "facilities": {
                "weight": 0.15,
                "query_by": "h3",
                "score_table": "amenities/facilities_scores_h3.parquet",
                "indexed_table": "amenities/facilities_indexed.parquet",
            },
        },
    },
    "building": {
        "label": "Building",
        "map_driving": False,
        "detail_rankable": False,
        "signals": ["housing_violations", "aep"],
        "weight_in_overall": 0.0,
        "sub_datasets": {
            "housing_violations": {
                "weight": 0.7,
                "query_by": "h3",
                "score_table": "building/housing_violations_scores_h3.parquet",
                "indexed_table": "building/housing_violations_indexed.parquet",
            },
            "aep": {
                "weight": 0.3,
                "query_by": "h3",
                "score_table": "building/aep_scores_h3.parquet",
                "indexed_table": "building/aep_indexed.parquet",
            },
        },
    },
}


def test_derived_config_matches_the_legacy_literal():
    """No score moves because of the refactor."""
    assert CATEGORY_CONFIG == LEGACY_CATEGORY_CONFIG


def test_category_key_order_is_preserved():
    """Some consumers iterate categories and present them in order."""
    assert list(CATEGORY_CONFIG) == list(LEGACY_CATEGORY_CONFIG)


def test_signal_order_is_preserved_within_each_category():
    for category_id, legacy in LEGACY_CATEGORY_CONFIG.items():
        assert CATEGORY_CONFIG[category_id]["signals"] == legacy["signals"]
        assert list(CATEGORY_CONFIG[category_id]["sub_datasets"]) == list(
            legacy["sub_datasets"]
        )


def test_signal_to_category_map_still_resolves_every_metric():
    mapping = signal_to_category_map()
    assert len(mapping) == len(METRICS)
    for metric in METRICS:
        assert mapping[metric.id] == metric.category


# --- registry integrity -----------------------------------------------------


def test_metric_ids_are_unique():
    ids = [m.id for m in METRICS]
    assert len(ids) == len(set(ids))


def test_every_metric_belongs_to_a_declared_category():
    for metric in METRICS:
        assert metric.category in CATEGORIES_BY_ID


def test_category_internal_weights_sum_to_one():
    """A category whose parts do not sum to 1 is silently rescaled at runtime."""
    for category in CATEGORIES:
        total = sum(m.weight_in_category for m in metrics_for_category(category.id))
        assert total == pytest.approx(1.0), f"{category.id} sums to {total}"


def test_scoring_categories_sum_to_one_across_overall():
    total = sum(c.weight_in_overall for c in CATEGORIES)
    assert total == pytest.approx(1.0)


def test_building_carries_no_weight_in_overall():
    """Pinned deliberately: this is a known open decision, not an oversight.

    If building becomes a fourth dimension the weights of the other three must
    move too, and this test should fail loudly when that happens.
    """
    assert CATEGORIES_BY_ID["building"].weight_in_overall == 0.0


def test_every_metric_is_documented():
    """The whole point of the registry is that these fields are never blank."""
    for metric in METRICS:
        assert metric.label.strip()
        assert metric.description.strip()
        assert metric.unit.strip()
        assert metric.source_dataset.strip()
        assert metric.source_relpath.strip()
        assert metric.methodology_version.strip()


def test_score_tables_are_namespaced_under_their_category():
    """A metric pointing at another category's parquet is a copy-paste bug."""
    for metric in METRICS:
        assert metric.score_table.startswith(f"{metric.category}/")
        if metric.indexed_table is not None:
            assert metric.indexed_table.startswith(f"{metric.category}/")


def test_query_by_follows_spatial_grain():
    for metric in METRICS:
        expected = "h3" if metric.spatial_grain.value == "h3_r9" else "zip"
        assert metric.query_by == expected


# --- the double count -------------------------------------------------------


def test_collision_transport_is_declared_as_a_copy_not_a_measurement():
    """The transit collision table is byte-identical to the safety one.

    `preprocess_common.py` writes the same grouped frame to both paths via a
    'score_copy' extra output. Recording that in the registry is what stops the
    duplication from reading as two independent corroborating signals.
    """
    transport = METRICS_BY_ID["collision_transport"]
    assert transport.derived_from == "collision"
    assert transport.source_relpath == METRICS_BY_ID["collision"].source_relpath


def test_duplicated_sources_reports_the_collision_pair():
    duplicates = duplicated_sources()
    assert duplicates == {
        "safety/motor_vehicle_collisions.csv": ("collision", "collision_transport")
    }


def test_the_collision_double_count_is_nineteen_percent_of_overall():
    """Pins the size of the problem so a fix has a number to move.

    safety 0.40 * 0.25 = 0.10, transit 0.30 * 0.30 = 0.09.
    """
    assert overall_contribution("collision") == pytest.approx(0.10)
    assert overall_contribution("collision_transport") == pytest.approx(0.09)
    combined = overall_contribution("collision") + overall_contribution(
        "collision_transport"
    )
    assert combined == pytest.approx(0.19)


def test_the_311_filter_admits_rodent_complaints_so_the_overlap_is_declared():
    """`311_sanitation` is not only sanitation.

    Its filter keeps complaint types RODENT, SANITATION CONDITION and
    UNSANITARY CONDITION, so it counts rat reports while `rodent` counts
    confirmed rat inspections. Both sit in safety at 0.20. That is not a copy
    -- the sources genuinely differ -- but the weights partly stack, so the
    registry has to say so.
    """
    sanitation = METRICS_BY_ID["311_sanitation"]
    rodent = METRICS_BY_ID["rodent"]
    assert "rodent" in sanitation.overlaps_with
    assert "311_sanitation" in rodent.overlaps_with
    assert sanitation.source_relpath != rodent.source_relpath
    assert sanitation.derived_from is None


def test_overlap_declarations_are_symmetric():
    """Reading either metric alone must disclose the overlap."""
    for metric in METRICS:
        for other_id in metric.overlaps_with:
            assert other_id in METRICS_BY_ID, other_id
            assert metric.id in METRICS_BY_ID[other_id].overlaps_with, (
                f"{other_id} does not declare its overlap with {metric.id}"
            )


def test_no_metric_declares_an_overlap_with_itself():
    for metric in METRICS:
        assert metric.id not in metric.overlaps_with


def test_overlapping_pairs_lists_each_pair_once():
    assert overlapping_pairs() == (("311_sanitation", "rodent"),)


def test_rodent_signal_reaches_forty_percent_of_the_safety_category():
    """Pins the stacked weight so item 1.3 has a number to argue against."""
    combined = (
        METRICS_BY_ID["rodent"].weight_in_category
        + METRICS_BY_ID["311_sanitation"].weight_in_category
    )
    assert combined == pytest.approx(0.40)


def test_a_derived_metric_points_at_a_metric_that_exists():
    for metric in METRICS:
        if metric.derived_from is not None:
            assert metric.derived_from in METRICS_BY_ID


def test_derived_metrics_keep_the_direction_of_their_source():
    """A copy that flipped polarity would score the same data both ways."""
    for metric in METRICS:
        if metric.derived_from is not None:
            assert metric.direction is METRICS_BY_ID[metric.derived_from].direction


# --- serialisation ----------------------------------------------------------


def test_registry_serialises_to_json():
    payload = registry_to_dict()
    json.dumps(payload)  # raises if anything is a bare Enum or dataclass


def test_registry_payload_reports_the_methodology_version_everywhere():
    payload = registry_to_dict()
    assert payload["methodology_version"] == METHODOLOGY_VERSION
    for entry in payload["metrics"]:
        assert entry["methodology_version"]


def test_registry_payload_covers_every_metric_and_category():
    payload = registry_to_dict()
    assert {m["id"] for m in payload["metrics"]} == set(METRICS_BY_ID)
    assert {c["id"] for c in payload["categories"]} == set(CATEGORIES_BY_ID)


def test_registry_payload_exposes_the_combined_duplicate_contribution():
    payload = registry_to_dict()
    assert payload["duplicated_sources"] == [
        {
            "source_relpath": "safety/motor_vehicle_collisions.csv",
            "metrics": ["collision", "collision_transport"],
            "combined_overall_contribution": 0.19,
        }
    ]


def test_registry_payload_exposes_the_overlapping_pair():
    payload = registry_to_dict()
    assert payload["overlapping_metrics"] == [
        {
            "metrics": ["311_sanitation", "rodent"],
            "combined_overall_contribution": 0.16,
        }
    ]


def test_directions_are_the_declared_enum():
    """Guards against a plain string sneaking in and comparing unequal."""
    for metric in METRICS:
        assert isinstance(metric.direction, Direction)


def test_risk_metrics_point_the_right_way():
    """Spot-check polarity against domain meaning, not against the code."""
    lower_is_better = {
        "collision",
        "collision_transport",
        "rodent",
        "311_sanitation",
        "ems_response",
        "fire_response",
        "housing_violations",
        "aep",
    }
    for metric in METRICS:
        expected = (
            Direction.LOWER_IS_BETTER
            if metric.id in lower_is_better
            else Direction.HIGHER_IS_BETTER
        )
        assert metric.direction is expected, metric.id
