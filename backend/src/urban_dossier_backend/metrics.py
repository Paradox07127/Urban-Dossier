"""The registry of what every score means.

Until now the answer to "what is this number?" was spread across four places:
[`categories.py`](categories.py) held weights and parquet paths,
`backend/scripts/preprocess_common.py` held the direction of each metric and
the normalisation applied to it, [`secondary_scoring.py`](secondary_scoring.py)
held the fallback formulas, and the units existed only in whoever's head last
read the preprocessing code. Nothing tied a number on the map back to its
definition, and nothing recorded which version of the method produced it.

This module is that tie. Every sub-metric that can reach a user gets one
`MetricDefinition` carrying its definition, unit, direction, spatial and
temporal grain, normalisation, source dataset, and methodology version. The
legacy `CATEGORY_CONFIG` shape is *derived* from these definitions rather than
maintained beside them, so the two cannot drift apart -- there is exactly one
place to change a weight.

Field choices follow the OECD/JRC *Handbook on Constructing Composite
Indicators*, whose ten-step framework treats documentation and metadata as
part of the method rather than as commentary on it. Two conventions are worth
naming because they are easy to get wrong:

*   **Direction is a property of the metric, not of the formula.** More
    collisions is worse; more trees is better. The preprocessing layer encodes
    this as ``access_mode``, which reads backwards for risk metrics. Here it is
    an explicit `Direction`, and the scoring layer can assert against it.
*   **Grain is disclosed, never silently upgraded.** Four metrics are only
    available per ZIP code. Their score is attached to every H3 cell in that
    ZIP, which is a join, not a measurement. Recording `SpatialGrain.ZIP`
    against them keeps that visible; AARP's Livability Index takes the same
    position of disclosing coarse geography rather than interpolating it away.

Provenance is deliberately concrete: `source_relpath` is the actual raw CSV the
preprocessing driver reads, so a reader can go from a number on screen to the
file it came from without guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# Bumped when a change alters the numbers a metric produces. Reported alongside
# every score so a screenshot taken today can be explained six months from now.
# Tracks the preprocessing release that introduced percentile normalisation.
METHODOLOGY_VERSION = "3.7.8"


class Direction(str, Enum):
    """Which way is good.

    ``HIGHER_IS_BETTER`` corresponds to ``access_mode=True`` in the
    preprocessing driver, ``LOWER_IS_BETTER`` to ``access_mode=False``.
    """

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class SpatialGrain(str, Enum):
    """The finest geography at which the metric is actually measured.

    Not the geography it is *displayed* at. A ZIP-grain metric rendered on H3
    cells is still ZIP-grain, and saying so is the point of the field.
    """

    H3_R9 = "h3_r9"
    ZIP = "zip"


class TemporalGrain(str, Enum):
    """How often the source refreshes.

    ``IRREGULAR`` covers the infrastructure inventories (subway entrances, bus
    shelters, kiosks, toilets, bike routes) which are republished when the city
    revises them rather than on a schedule. It is used in preference to
    inventing a cadence.
    """

    DAILY = "daily"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


class Normalization(str, Enum):
    """How a raw quantity became a 0-100 score.

    ``EMPIRICAL_PERCENTILE`` is ``percentile_score()``: rank within the observed
    NYC distribution. It replaced a linear clip formula in v3.7.8 because the
    clip collapsed dense areas to 0 and sparse ones to 100. The handbook treats
    percentile ranking as an accepted normalisation method, so this is a
    documented choice rather than a stopgap.

    ``COMPOSITE_PERCENTILE`` marks the one metric built from two percentile
    series combined before scoring (restaurant abundance and inspection
    quality).
    """

    EMPIRICAL_PERCENTILE = "empirical_percentile"
    COMPOSITE_PERCENTILE = "composite_percentile"


@dataclass(frozen=True)
class MetricDefinition:
    """One sub-metric, fully described."""

    id: str
    category: str
    label: str
    description: str
    unit: str
    direction: Direction
    spatial_grain: SpatialGrain
    temporal_grain: TemporalGrain
    normalization: Normalization
    weight_in_category: float
    source_dataset: str
    source_relpath: str
    score_table: str
    indexed_table: str | None = None
    # Keys this metric reads out of the analyse-point ``current_state`` payload.
    # Ties the registry to the runtime rather than leaving it a parallel
    # document, and lets a test assert the fallback formulas stay in sync.
    state_keys: tuple[str, ...] = ()
    methodology_version: str = METHODOLOGY_VERSION
    # Set when this metric's score table is a copy of another metric's, rather
    # than an independent measurement. See ``collision_transport``.
    derived_from: str | None = None
    # Metrics measuring overlapping phenomena from different sources. Weaker
    # than ``derived_from``: these are genuinely separate measurements that
    # nonetheless partly count the same thing, so their weights partly stack.
    # Declared here rather than described in prose so the correlation work in
    # item 1.3 has a machine-readable list of pairs to test.
    overlaps_with: tuple[str, ...] = ()
    notes: str | None = None

    @property
    def query_by(self) -> str:
        """The lookup key the provider uses. Mirrors `SpatialGrain`."""
        return "h3" if self.spatial_grain is SpatialGrain.H3_R9 else "zip"


@dataclass(frozen=True)
class CategoryDefinition:
    """One scoring category."""

    id: str
    label: str
    weight_in_overall: float
    map_driving: bool
    detail_rankable: bool
    notes: str | None = None


CATEGORIES: tuple[CategoryDefinition, ...] = (
    CategoryDefinition(
        id="safety",
        label="Safety",
        weight_in_overall=0.40,
        map_driving=True,
        detail_rankable=True,
    ),
    CategoryDefinition(
        id="transit",
        label="Transit",
        weight_in_overall=0.30,
        map_driving=True,
        detail_rankable=True,
        notes=(
            "Only collision_transport has a fallback formula; subway and bus "
            "resolve to None when prepared score tables are absent. A fallback "
            "transit score is therefore a road-safety measure wearing a "
            "transit label."
        ),
    ),
    CategoryDefinition(
        id="amenities",
        label="Amenities",
        weight_in_overall=0.30,
        map_driving=True,
        detail_rankable=True,
    ),
    CategoryDefinition(
        id="building",
        label="Building",
        weight_in_overall=0.0,
        map_driving=False,
        detail_rankable=False,
        notes=(
            "Weight 0: building scores are computed and shown but never reach "
            "overall, and cannot be raised by user priority either. Whether "
            "this becomes a fourth dimension or an independent risk flag is an "
            "open product decision (PROJECT_PLAN P0-02)."
        ),
    ),
)


METRICS: tuple[MetricDefinition, ...] = (
    # ---- safety ------------------------------------------------------------
    MetricDefinition(
        id="collision",
        category="safety",
        label="Traffic collisions",
        description=(
            "Reported motor vehicle collisions near the location, weighted "
            "further down by pedestrian and cyclist injuries."
        ),
        unit="collisions within 500 m",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.25,
        source_dataset="NYPD Motor Vehicle Collisions - Crashes",
        source_relpath="safety/motor_vehicle_collisions.csv",
        score_table="safety/collisions_scores_h3.parquet",
        indexed_table="safety/collisions_indexed.parquet",
        state_keys=("collision_count_500m", "ped_cyclist_injuries_1km"),
    ),
    MetricDefinition(
        id="rodent",
        category="safety",
        label="Rodent activity",
        description="Inspections that found active rodent signs.",
        unit="rodent-positive inspections within 500 m",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.20,
        source_dataset="DOHMH Rodent Inspection",
        source_relpath="environment/rodent_inspections.csv",
        score_table="safety/rodent_scores_h3.parquet",
        indexed_table="safety/rodent_indexed.parquet",
        state_keys=("rodent_positive_500m",),
        overlaps_with=("311_sanitation",),
        notes=(
            "Inspection outcomes, not complaints: rows whose RESULT mentions "
            "rats, a failed inspection or active signs. See `311_sanitation`, "
            "which counts the resident-reported side of the same phenomenon."
        ),
    ),
    MetricDefinition(
        id="311_sanitation",
        category="safety",
        label="Sanitation and rodent complaints",
        description=(
            "311 service requests of type RODENT, SANITATION CONDITION or "
            "UNSANITARY CONDITION -- what residents reported, as distinct from "
            "what inspectors confirmed."
        ),
        unit="sanitation and rodent 311 requests within 500 m",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.20,
        source_dataset="311 Service Requests (2020 - Present)",
        source_relpath="quality_of_life/311_service_requests_2020_present.csv",
        score_table="safety/311_scores_h3.parquet",
        indexed_table="safety/311_safety_indexed.parquet",
        state_keys=("sanitation_311_recent_count",),
        overlaps_with=("rodent",),
        notes=(
            "Despite the id, the filter admits RODENT complaints alongside the "
            "two sanitation types, so this metric and `rodent` both count rat "
            "activity -- one as resident reports, one as confirmed "
            "inspections. Both sit in safety at weight 0.20, so rodent "
            "activity carries up to 0.40 of the category between them. How "
            "much they actually co-move is what item 1.3 measures. Separately, "
            "the fallback formula scores this with a bare multiplier "
            "(count * 2.5, capped at 55) rather than against a measured "
            "baseline, unlike every other safety sub-metric."
        ),
    ),
    MetricDefinition(
        id="ems_response",
        category="safety",
        label="EMS response time",
        description="Average ambulance dispatch-to-arrival time for the ZIP code.",
        unit="mean response seconds (ZIP)",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.ZIP,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.20,
        source_dataset="EMS Incident Dispatch Data",
        source_relpath="safety/ems_incident_dispatch.csv",
        score_table="safety/ems_scores_zip.parquet",
        state_keys=("ems_avg_response_seconds",),
    ),
    MetricDefinition(
        id="fire_response",
        category="safety",
        label="Fire response time",
        description="Average fire unit dispatch-to-arrival time for the ZIP code.",
        unit="mean response seconds (ZIP)",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.ZIP,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.15,
        source_dataset="Fire Incident Dispatch Data",
        source_relpath="safety/fire_incident_dispatch.csv",
        score_table="safety/fire_scores_zip.parquet",
        state_keys=("fire_avg_response_seconds",),
    ),
    # ---- transit -----------------------------------------------------------
    MetricDefinition(
        id="collision_transport",
        category="transit",
        label="Road safety for travel",
        description=(
            "The collision picture read as a travel-risk signal rather than a "
            "neighbourhood-safety one."
        ),
        unit="collisions within 500 m",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.30,
        source_dataset="NYPD Motor Vehicle Collisions - Crashes",
        source_relpath="safety/motor_vehicle_collisions.csv",
        score_table="transit/collision_transport_scores_h3.parquet",
        indexed_table="transit/collision_transport_indexed.parquet",
        state_keys=("collision_count_500m", "ped_cyclist_injuries_1km"),
        derived_from="collision",
        notes=(
            "Not an independent measurement. The preprocessing driver writes "
            "the collision score table to this path unchanged (a 'score_copy' "
            "extra output), so the two parquet files are byte-identical. The "
            "same dataset therefore reaches `overall` twice: once at "
            "0.40 * 0.25 and again at 0.30 * 0.30, together 19% of the "
            "composite. Resolving that double count is work item 1.3."
        ),
    ),
    MetricDefinition(
        id="subway",
        category="transit",
        label="Subway access",
        description="Subway entrances and exits reachable near the location.",
        unit="subway entrances within 500 m",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.25,
        source_dataset="MTA Subway Entrances and Exits (2024)",
        source_relpath="transit/mta_subway_entrances_exits_2024.csv",
        score_table="transit/subway_scores_h3.parquet",
        indexed_table="transit/subway_indexed.parquet",
        notes="No fallback formula; resolves to None without a prepared score table.",
    ),
    MetricDefinition(
        id="bus",
        category="transit",
        label="Bus access",
        description="Sheltered bus stops near the location.",
        unit="bus shelters within 500 m",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.20,
        source_dataset="Bus Stop Shelters",
        source_relpath="transit/bus_stop_shelters.csv",
        score_table="transit/bus_scores_h3.parquet",
        indexed_table="transit/bus_indexed.parquet",
        notes="No fallback formula; resolves to None without a prepared score table.",
    ),
    MetricDefinition(
        id="bike_routes",
        category="transit",
        label="Bike network",
        description=(
            "Designated cycling routes, measured by how much route geometry "
            "passes through the cell."
        ),
        unit="bike route vertices per cell",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.15,
        source_dataset="NYC Bike Routes",
        source_relpath="transit/nyc_bike_routes.csv",
        score_table="transit/bike_routes_scores_h3.parquet",
        indexed_table="transit/bike_routes_indexed.parquet",
        notes=(
            "Line geometry is sampled to vertices before H3 indexing, so the "
            "count reflects route density and vertex spacing together, not "
            "route length in metres."
        ),
    ),
    MetricDefinition(
        id="open_streets",
        category="transit",
        label="Open Streets",
        description="Street segments in the Open Streets programme.",
        unit="Open Streets vertices per cell",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.10,
        source_dataset="Open Streets Locations",
        source_relpath="transit/open_streets_locations.csv",
        score_table="transit/open_streets_scores_h3.parquet",
        indexed_table="transit/open_streets_indexed.parquet",
        notes="Vertex-sampled like bike_routes; same caveat applies.",
    ),
    # ---- amenities ---------------------------------------------------------
    MetricDefinition(
        id="parks_access",
        category="amenities",
        label="Park access",
        description="Total parkland acreage in the ZIP code.",
        unit="park acres (ZIP total)",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.ZIP,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.25,
        source_dataset="Parks Properties",
        source_relpath="amenities/parks_properties.csv",
        score_table="amenities/parks_scores_zip.parquet",
        state_keys=("park_acres_zip_proxy",),
        notes=(
            "ZIP-total acreage, not distance to the nearest park. A large park "
            "at the far edge of a ZIP scores the same as one across the street."
        ),
    ),
    MetricDefinition(
        id="trees",
        category="amenities",
        label="Street trees",
        description="Living street trees near the location.",
        unit="living street trees within 500 m",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.15,
        source_dataset="Street Tree Census",
        source_relpath="amenities/street_trees.csv",
        score_table="amenities/trees_scores_h3.parquet",
        indexed_table="amenities/trees_indexed.parquet",
        state_keys=("tree_count_500m",),
        notes="Only records whose status is ALIVE are counted; stumps and dead trees are dropped.",
    ),
    MetricDefinition(
        id="public_toilets",
        category="amenities",
        label="Public toilets",
        description="Operational public restrooms within walking distance.",
        unit="operational public toilets within 1 km",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.15,
        source_dataset="Public Restrooms",
        source_relpath="amenities/public_toilets.csv",
        score_table="amenities/toilets_scores_h3.parquet",
        indexed_table="amenities/toilets_indexed.parquet",
        state_keys=("toilet_count_1km",),
    ),
    MetricDefinition(
        id="linknyc",
        category="amenities",
        label="LinkNYC kiosks",
        description="Live LinkNYC kiosks providing public wifi and calls.",
        unit="live kiosks within 500 m",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.10,
        source_dataset="LinkNYC Kiosk Locations",
        source_relpath="amenities/linknyc_kiosk_locations.csv",
        score_table="amenities/linknyc_scores_h3.parquet",
        indexed_table="amenities/linknyc_indexed.parquet",
        state_keys=("linknyc_count_500m",),
    ),
    MetricDefinition(
        id="restaurant_context",
        category="amenities",
        label="Food establishments",
        description=(
            "How many distinct food establishments are nearby, adjusted down "
            "where a high share of their inspections found critical violations."
        ),
        unit="distinct establishments within 500 m, critical-violation adjusted",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.COMPOSITE_PERCENTILE,
        weight_in_category=0.20,
        source_dataset="DOHMH New York City Restaurant Inspection Results",
        source_relpath="amenities/dohmh_restaurant_inspections.csv",
        score_table="amenities/restaurants_scores_h3.parquet",
        indexed_table="amenities/restaurants_indexed.parquet",
        state_keys=("restaurant_count_500m", "restaurant_critical_rate_500m"),
        notes=(
            "Counted by distinct CAMIS so a restaurant inspected six times "
            "counts once. Two percentile series -- abundance and critical rate "
            "-- are combined before scoring, which is why the normalisation is "
            "marked composite."
        ),
    ),
    MetricDefinition(
        id="facilities",
        category="amenities",
        label="Public facilities",
        description=(
            "Civic facilities from the city facilities database: libraries, "
            "community centres, clinics and similar."
        ),
        unit="facilities within 500 m",
        direction=Direction.HIGHER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.IRREGULAR,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.15,
        source_dataset="Facilities Database",
        source_relpath="amenities/facilities_database.csv",
        score_table="amenities/facilities_scores_h3.parquet",
        indexed_table="amenities/facilities_indexed.parquet",
        notes="No fallback formula; resolves to None without a prepared score table.",
    ),
    # ---- building ----------------------------------------------------------
    MetricDefinition(
        id="housing_violations",
        category="building",
        label="Housing violations",
        description="Open HPD housing maintenance violations on nearby buildings.",
        unit="open class B and C violations within 250 m",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.DAILY,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.70,
        source_dataset="HPD Housing Maintenance Code Violations",
        source_relpath="buildings/housing_code_violations.csv",
        score_table="building/housing_violations_scores_h3.parquet",
        indexed_table="building/housing_violations_indexed.parquet",
        state_keys=("open_class_c_250m", "open_class_b_250m"),
        notes="Only violations still open at snapshot time are counted.",
    ),
    MetricDefinition(
        id="aep",
        category="building",
        label="Alternative Enforcement Program",
        description=(
            "Buildings placed in the city's Alternative Enforcement Program, "
            "which targets the most distressed multiple dwellings."
        ),
        unit="active AEP buildings within 250 m",
        direction=Direction.LOWER_IS_BETTER,
        spatial_grain=SpatialGrain.H3_R9,
        temporal_grain=TemporalGrain.ANNUAL,
        normalization=Normalization.EMPIRICAL_PERCENTILE,
        weight_in_category=0.30,
        source_dataset="Buildings Selected for AEP",
        source_relpath="buildings/buildings_aep.csv",
        score_table="building/aep_scores_h3.parquet",
        indexed_table="building/aep_indexed.parquet",
        state_keys=("aep_count_250m",),
    ),
)


# --- lookups ----------------------------------------------------------------

METRICS_BY_ID: dict[str, MetricDefinition] = {m.id: m for m in METRICS}
CATEGORIES_BY_ID: dict[str, CategoryDefinition] = {c.id: c for c in CATEGORIES}


def metrics_for_category(category_id: str) -> tuple[MetricDefinition, ...]:
    """Every metric in one category, in registry order."""
    return tuple(m for m in METRICS if m.category == category_id)


def overall_contribution(metric_id: str) -> float:
    """This metric's share of the default `overall` score.

    The product of its category's weight in overall and its own weight within
    the category. Makes double counting arithmetic rather than argument: sum
    the contributions of every metric sharing a `source_relpath` and the
    duplication is a number.
    """
    metric = METRICS_BY_ID[metric_id]
    return CATEGORIES_BY_ID[metric.category].weight_in_overall * metric.weight_in_category


def duplicated_sources() -> dict[str, tuple[str, ...]]:
    """Raw files feeding more than one metric, keyed by source path.

    Used by the methodology page and by the correlation work in item 1.3.
    """
    by_source: dict[str, list[str]] = {}
    for metric in METRICS:
        by_source.setdefault(metric.source_relpath, []).append(metric.id)
    return {src: tuple(ids) for src, ids in by_source.items() if len(ids) > 1}


def overlapping_pairs() -> tuple[tuple[str, str], ...]:
    """Declared overlapping metric pairs, each listed once, sorted.

    `overlaps_with` is declared on both sides so either metric can be read on
    its own and still disclose the overlap. This collapses that to one entry
    per pair, which is what a correlation run wants as input.
    """
    pairs: set[tuple[str, str]] = set()
    for metric in METRICS:
        for other in metric.overlaps_with:
            pairs.add(tuple(sorted((metric.id, other))))  # type: ignore[arg-type]
    return tuple(sorted(pairs))


def build_category_config() -> dict:
    """Reproduce the legacy `CATEGORY_CONFIG` shape from the registry.

    Kept byte-compatible with the hand-maintained dict this replaced so no
    consumer had to change; `test_metric_registry.py` pins that equality
    against a frozen copy.
    """
    config: dict = {}
    for category in CATEGORIES:
        members = metrics_for_category(category.id)
        sub_datasets: dict = {}
        for metric in members:
            entry: dict = {
                "weight": metric.weight_in_category,
                "query_by": metric.query_by,
                "score_table": metric.score_table,
            }
            if metric.indexed_table is not None:
                entry["indexed_table"] = metric.indexed_table
            sub_datasets[metric.id] = entry
        config[category.id] = {
            "label": category.label,
            "map_driving": category.map_driving,
            "detail_rankable": category.detail_rankable,
            "signals": [m.id for m in members],
            "weight_in_overall": category.weight_in_overall,
            "sub_datasets": sub_datasets,
        }
    return config


def metric_to_dict(metric: MetricDefinition) -> dict:
    """Serialise one metric for the API."""
    return {
        "id": metric.id,
        "category": metric.category,
        "label": metric.label,
        "description": metric.description,
        "unit": metric.unit,
        "direction": metric.direction.value,
        "spatial_grain": metric.spatial_grain.value,
        "temporal_grain": metric.temporal_grain.value,
        "normalization": metric.normalization.value,
        "weight_in_category": metric.weight_in_category,
        "overall_contribution": round(overall_contribution(metric.id), 4),
        "source_dataset": metric.source_dataset,
        "source_relpath": metric.source_relpath,
        "score_table": metric.score_table,
        "state_keys": list(metric.state_keys),
        "methodology_version": metric.methodology_version,
        "derived_from": metric.derived_from,
        "overlaps_with": list(metric.overlaps_with),
        "notes": metric.notes,
    }


def registry_to_dict() -> dict:
    """The whole registry, for `GET /api/metrics`."""
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "categories": [
            {
                "id": c.id,
                "label": c.label,
                "weight_in_overall": c.weight_in_overall,
                "map_driving": c.map_driving,
                "detail_rankable": c.detail_rankable,
                "metrics": [m.id for m in metrics_for_category(c.id)],
                "notes": c.notes,
            }
            for c in CATEGORIES
        ],
        "metrics": [metric_to_dict(m) for m in METRICS],
        "duplicated_sources": [
            {
                "source_relpath": src,
                "metrics": list(ids),
                "combined_overall_contribution": round(
                    sum(overall_contribution(i) for i in ids), 4
                ),
            }
            for src, ids in sorted(duplicated_sources().items())
        ],
        # Same idea one step weaker: different sources, overlapping subject.
        "overlapping_metrics": [
            {
                "metrics": list(pair),
                "combined_overall_contribution": round(
                    sum(overall_contribution(i) for i in pair), 4
                ),
            }
            for pair in overlapping_pairs()
        ],
    }
