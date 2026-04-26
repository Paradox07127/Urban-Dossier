from __future__ import annotations


CATEGORY_CONFIG = {
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

DEFAULT_PRIORITY_ORDER = ["amenities", "transit", "safety"]


def signal_to_category_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for category_id, cfg in CATEGORY_CONFIG.items():
        for signal in cfg["signals"]:
            mapping[signal] = category_id
    return mapping
