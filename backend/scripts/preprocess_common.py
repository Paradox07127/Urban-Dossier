"""Urban Dossier v3.7.8 preprocessing driver.

This module converts raw NYC Open Data CSVs into the ready layer described in
`urban-dossier-v3.7.7-engineering.md`.

Scoring model (v3.7.8 change)
-----------------------------
Earlier releases used a hard-coded linear formula:

    risk_score   = clip(100 - raw_count * 2, 0, 100)
    access_score = clip(40  + raw_count * 5, 0, 100)

In high-density NYC H3 cells this collapsed 100% of Manhattan collisions to
score=0 and 100% of street-tree cells to score=100, which destroyed any
visual gradient on the map. v3.7.8 replaces it with a percentile-rank based
score: every dataset's ``score`` column is uniformly distributed across
0..100 so downstream map layers always have a usable gradient, regardless
of how dense or sparse the raw metric is.

Dataset-specific fixes bundled with this release:
  * Parks (zip_sum): score is now derived from total park acreage per ZIP
    (total_value) rather than the number of park records.
  * Restaurants (point_count): CRITICAL_FLAG is now read through to emit a
    per-cell critical_count/critical_rate and a risk-adjusted score.
  * Collisions: emits an additional ``transit/collision_transport_*`` view
    so the transit direction gets its own score table (v3.7.7 §6).
  * Bike routes & Open Streets: WKT MULTILINESTRING parsing so transit
    direction is no longer just subway+bus.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import cudf.pandas
    cudf.pandas.install()
except ImportError:
    pass

import pandas as pd
from h3 import latlng_to_cell


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = Path.home() / "nyc_open_data"
DEFAULT_READY_ROOT = REPO_ROOT / "data" / "ready"
PARQUET_COMPRESSION = os.getenv("URBAN_DOSSIER_PARQUET_COMPRESSION", "zstd")
PARQUET_COMPRESSION_LEVEL = int(os.getenv("URBAN_DOSSIER_PARQUET_COMPRESSION_LEVEL", "3"))
PARQUET_ROW_GROUP_ROWS = int(os.getenv("URBAN_DOSSIER_PARQUET_ROW_GROUP_ROWS", "250000"))

# Rough NYC bounding box (five boroughs + a small margin). Applied in
# _prepare_dataframe so any upstream row with a typoed lat/lon (lat=0,
# lat=34.78, lon=158, etc) is dropped before H3 encoding; otherwise those
# rows produce phantom overview cells in Alabama or the Indian Ocean.
NYC_BBOX = {
    "lat_min": 40.45,
    "lat_max": 40.95,
    "lon_min": -74.30,
    "lon_max": -73.65,
}


# ----------------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------------

def normalize_zip(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return digits[:5].zfill(5)


def normalize_borough(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().upper()
    mapping = {
        "MN": "MANHATTAN",
        "BK": "BROOKLYN",
        "BX": "BRONX",
        "QN": "QUEENS",
        "SI": "STATEN ISLAND",
    }
    return mapping.get(text, text)


def safe_datetime(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def add_h3(df: pd.DataFrame, lat_col: str = "latitude", lon_col: str = "longitude", resolution: int = 9) -> pd.DataFrame:
    df = df.copy()
    df["h3_r9"] = [
        latlng_to_cell(float(lat), float(lon), resolution)
        if pd.notna(lat) and pd.notna(lon)
        else None
        for lat, lon in zip(df[lat_col], df[lon_col])
    ]
    return df


def quarter_label(series: pd.Series) -> pd.Series:
    dt = safe_datetime(series)
    return dt.dt.to_period("Q").astype("string")


# ----------------------------------------------------------------------------
# Scoring - percentile-rank based (v3.7.8)
# ----------------------------------------------------------------------------

def percentile_score(series: pd.Series, access_mode: bool) -> pd.Series:
    """Map a numeric series to a 0..100 score via empirical percentile rank.

    * access_mode=True  -> more is better (tree count, transit access)
    * access_mode=False -> more is worse (collisions, violations, rodents)
    """
    if series.empty:
        return series.astype("int64")
    values = pd.to_numeric(series, errors="coerce").fillna(0)
    rank_pct = values.rank(method="average", pct=True) * 100
    if not access_mode:
        rank_pct = 100 - rank_pct
    return rank_pct.round().clip(lower=0, upper=100).astype(int)


def response_time_score(avg_seconds: pd.Series) -> pd.Series:
    """Emergency response scoring: faster = better.

    Uses percentile rank (same as point_count datasets) so the full 0-100
    range is used.  The old linear formula ``100 - avg/p75*40`` collapsed
    all ZIPs to a 60±5 band because NYC response times have low variance.
    """
    # Slower response = worse = lower score.  That's access_mode=False
    # (higher value → lower score), which percentile_score handles.
    return percentile_score(avg_seconds, access_mode=False)


# Backwards-compatible aliases kept to avoid breaking any external caller.
def risk_score_from_count(series: pd.Series, scale: float = 2.0) -> pd.Series:  # noqa: ARG001 - scale ignored
    return percentile_score(series, access_mode=False)


def access_score_from_count(series: pd.Series, base: float = 40.0, scale: float = 5.0) -> pd.Series:  # noqa: ARG001
    return percentile_score(series, access_mode=True)


# ----------------------------------------------------------------------------
# Dataset specs
# ----------------------------------------------------------------------------

@dataclass
class DatasetSpec:
    name: str
    raw_relpath: str
    output_dir: str
    indexed_name: str
    score_name: str
    trend_name: str | None = None
    dataset_type: str = "point_count"
    usecols: list[str] | None = None
    rename_map: dict[str, str] | None = None
    date_col: str | None = None
    lat_col: str | None = None
    lon_col: str | None = None
    zip_col: str | None = None
    borough_col: str | None = None
    value_col: str | None = None
    access_mode: bool = False
    filter_mode: str | None = None
    extra_outputs: list[dict[str, str]] = field(default_factory=list)
    wkt_col: str | None = None  # for line_vertices datasets
    critical_col: str | None = None  # restaurants only
    entity_col: str | None = None  # distinct physical entity for access counts


SPECS: dict[str, DatasetSpec] = {
    "safety_collisions": DatasetSpec(
        name="safety_collisions",
        raw_relpath="safety/motor_vehicle_collisions.csv",
        output_dir="safety",
        indexed_name="collisions_indexed.parquet",
        score_name="collisions_scores_h3.parquet",
        trend_name="collisions_quarterly_h3.parquet",
        usecols=["CRASH DATE", "LATITUDE", "LONGITUDE", "ZIP CODE", "BOROUGH", "COLLISION_ID", "ON STREET NAME", "NUMBER OF PEDESTRIANS INJURED", "NUMBER OF CYCLIST INJURED"],
        rename_map={"CRASH DATE": "event_date", "LATITUDE": "latitude", "LONGITUDE": "longitude", "ZIP CODE": "zip", "BOROUGH": "borough"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        # Until v3.8.0 a 'score_copy' extra output duplicated this dataset's
        # tables into transit/collision_transport_*. The copy fed a metric the
        # correlation analysis removed (rho = 1.000 with its source by
        # construction), so the copy step went with it. The machinery for
        # extra_outputs stays; nothing uses it at present.
    ),
    "safety_rodent": DatasetSpec(
        name="safety_rodent",
        raw_relpath="environment/rodent_inspections.csv",
        output_dir="safety",
        indexed_name="rodent_indexed.parquet",
        score_name="rodent_scores_h3.parquet",
        trend_name="rodent_quarterly_h3.parquet",
        usecols=["INSPECTION_DATE", "RESULT", "LATITUDE", "LONGITUDE", "ZIP_CODE", "BOROUGH", "JOB_ID"],
        rename_map={"INSPECTION_DATE": "event_date", "LATITUDE": "latitude", "LONGITUDE": "longitude", "ZIP_CODE": "zip", "BOROUGH": "borough"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        filter_mode="rodent_positive",
    ),
    "safety_311": DatasetSpec(
        name="safety_311",
        raw_relpath="quality_of_life/311_service_requests_2020_present.csv",
        output_dir="safety",
        indexed_name="311_safety_indexed.parquet",
        score_name="311_scores_h3.parquet",
        trend_name="311_quarterly_h3.parquet",
        usecols=["Unique Key", "Created Date", "Problem (formerly Complaint Type)", "Problem Detail (formerly Descriptor)", "Latitude", "Longitude", "Incident Zip", "Borough"],
        rename_map={"Created Date": "event_date", "Latitude": "latitude", "Longitude": "longitude", "Incident Zip": "zip", "Borough": "borough"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        filter_mode="safety_311",
    ),
    "safety_ems": DatasetSpec(
        name="safety_ems",
        raw_relpath="safety/ems_incident_dispatch.csv",
        output_dir="safety",
        indexed_name="ems_indexed.parquet",
        score_name="ems_scores_zip.parquet",
        dataset_type="zip_response",
        usecols=["ZIPCODE", "INCIDENT_RESPONSE_SECONDS_QY", "BOROUGH"],
        rename_map={"ZIPCODE": "zip", "INCIDENT_RESPONSE_SECONDS_QY": "response_seconds", "BOROUGH": "borough"},
        zip_col="zip",
        borough_col="borough",
        value_col="response_seconds",
    ),
    "safety_fire": DatasetSpec(
        name="safety_fire",
        raw_relpath="safety/fire_incident_dispatch.csv",
        output_dir="safety",
        indexed_name="fire_indexed.parquet",
        score_name="fire_scores_zip.parquet",
        dataset_type="zip_response",
        usecols=["ZIPCODE", "INCIDENT_RESPONSE_SECONDS_QY", "INCIDENT_BOROUGH"],
        rename_map={"ZIPCODE": "zip", "INCIDENT_RESPONSE_SECONDS_QY": "response_seconds", "INCIDENT_BOROUGH": "borough"},
        zip_col="zip",
        borough_col="borough",
        value_col="response_seconds",
    ),
    "amenities_restaurants": DatasetSpec(
        name="amenities_restaurants",
        raw_relpath="amenities/dohmh_restaurant_inspections.csv",
        output_dir="amenities",
        indexed_name="restaurants_indexed.parquet",
        score_name="restaurants_scores_h3.parquet",
        trend_name="restaurants_quarterly_h3.parquet",
        usecols=["CAMIS", "DBA", "INSPECTION DATE", "CRITICAL FLAG", "GRADE", "Latitude", "Longitude", "ZIPCODE", "BORO"],
        rename_map={"INSPECTION DATE": "event_date", "Latitude": "latitude", "Longitude": "longitude", "ZIPCODE": "zip", "BORO": "borough"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        access_mode=True,
        critical_col="CRITICAL FLAG",
        entity_col="CAMIS",
    ),
    "amenities_parks": DatasetSpec(
        name="amenities_parks",
        raw_relpath="amenities/parks_properties.csv",
        output_dir="amenities",
        indexed_name="parks_indexed.parquet",
        score_name="parks_scores_zip.parquet",
        dataset_type="zip_sum",
        usecols=["NAME311", "ACRES", "ZIPCODE", "BOROUGH", "WATERFRONT"],
        rename_map={"ZIPCODE": "zip", "BOROUGH": "borough", "ACRES": "value"},
        zip_col="zip",
        borough_col="borough",
        value_col="value",
        access_mode=True,
    ),
    "amenities_trees": DatasetSpec(
        name="amenities_trees",
        raw_relpath="amenities/street_trees.csv",
        output_dir="amenities",
        indexed_name="trees_indexed.parquet",
        score_name="trees_scores_h3.parquet",
        trend_name="trees_quarterly_h3.parquet",
        usecols=["tree_id", "created_at", "latitude", "longitude", "postcode", "borough", "status", "health"],
        rename_map={"created_at": "event_date", "postcode": "zip"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        access_mode=True,
        filter_mode="alive_tree",
    ),
    "amenities_linknyc": DatasetSpec(
        name="amenities_linknyc",
        raw_relpath="amenities/linknyc_kiosk_locations.csv",
        output_dir="amenities",
        indexed_name="linknyc_indexed.parquet",
        score_name="linknyc_scores_h3.parquet",
        usecols=["Site ID", "Street Address", "Installation Status", "Latitude", "Longitude", "Postcode", "Borough"],
        rename_map={"Latitude": "latitude", "Longitude": "longitude", "Postcode": "zip", "Borough": "borough"},
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        access_mode=True,
        filter_mode="live_linknyc",
    ),
    "amenities_toilets": DatasetSpec(
        name="amenities_toilets",
        raw_relpath="amenities/public_toilets.csv",
        output_dir="amenities",
        indexed_name="toilets_indexed.parquet",
        score_name="toilets_scores_h3.parquet",
        usecols=["Facility Name", "Status", "Latitude", "Longitude"],
        rename_map={"Latitude": "latitude", "Longitude": "longitude"},
        lat_col="latitude",
        lon_col="longitude",
        access_mode=True,
        filter_mode="operational_toilet",
    ),
    "amenities_facilities": DatasetSpec(
        name="amenities_facilities",
        raw_relpath="amenities/facilities_database.csv",
        output_dir="amenities",
        indexed_name="facilities_indexed.parquet",
        score_name="facilities_scores_h3.parquet",
        usecols=["uid", "facname", "facgroup", "facsubgrp", "zipcode", "boro", "latitude", "longitude"],
        rename_map={"zipcode": "zip", "boro": "borough"},
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        access_mode=True,
    ),
    "transit_subway": DatasetSpec(
        name="transit_subway",
        raw_relpath="transit/mta_subway_entrances_exits_2024.csv",
        output_dir="transit",
        indexed_name="subway_indexed.parquet",
        score_name="subway_scores_h3.parquet",
        usecols=["Stop Name", "Entrance Latitude", "Entrance Longitude", "Borough"],
        rename_map={"Entrance Latitude": "latitude", "Entrance Longitude": "longitude", "Borough": "borough"},
        lat_col="latitude",
        lon_col="longitude",
        borough_col="borough",
        access_mode=True,
    ),
    "transit_bus": DatasetSpec(
        name="transit_bus",
        raw_relpath="transit/bus_stop_shelters.csv",
        output_dir="transit",
        indexed_name="bus_indexed.parquet",
        score_name="bus_scores_h3.parquet",
        usecols=["Shelter_ID", "On_Street", "Cross_Stre", "Latitude", "Longitude", "BoroName"],
        rename_map={"Latitude": "latitude", "Longitude": "longitude", "BoroName": "borough"},
        lat_col="latitude",
        lon_col="longitude",
        borough_col="borough",
        access_mode=True,
    ),
    "transit_bike_routes": DatasetSpec(
        name="transit_bike_routes",
        raw_relpath="transit/nyc_bike_routes.csv",
        output_dir="transit",
        indexed_name="bike_routes_indexed.parquet",
        score_name="bike_routes_scores_h3.parquet",
        dataset_type="line_vertices",
        usecols=["the_geom", "segmentid", "status", "street", "boro", "allclasses"],
        wkt_col="the_geom",
        access_mode=True,
        filter_mode="current_bike_route",
    ),
    "transit_open_streets": DatasetSpec(
        name="transit_open_streets",
        raw_relpath="transit/open_streets_locations.csv",
        output_dir="transit",
        indexed_name="open_streets_indexed.parquet",
        score_name="open_streets_scores_h3.parquet",
        dataset_type="line_vertices",
        usecols=["Object ID", "Organization Name", "Approved On Street", "Borough Name", "The_Geom"],
        wkt_col="The_Geom",
        access_mode=True,
    ),
    "building_violations": DatasetSpec(
        name="building_violations",
        raw_relpath="buildings/housing_code_violations.csv",
        output_dir="building",
        indexed_name="housing_violations_indexed.parquet",
        score_name="housing_violations_scores_h3.parquet",
        trend_name="housing_violations_quarterly_h3.parquet",
        usecols=["ViolationID", "Class", "InspectionDate", "CurrentStatus", "ViolationStatus", "Latitude", "Longitude", "Postcode", "Borough", "BBL", "BIN"],
        rename_map={"InspectionDate": "event_date", "Postcode": "zip", "Latitude": "latitude", "Longitude": "longitude"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        filter_mode="open_violations",
    ),
    "building_aep": DatasetSpec(
        name="building_aep",
        raw_relpath="buildings/buildings_aep.csv",
        output_dir="building",
        indexed_name="aep_indexed.parquet",
        score_name="aep_scores_h3.parquet",
        usecols=["BUILDING_ID", "CURRENT_STATUS", "AEP_START_DATE", "Postcode", "BOROUGH", "Latitude", "Longitude", "BBL", "BIN"],
        rename_map={"AEP_START_DATE": "event_date", "Postcode": "zip", "BOROUGH": "borough", "Latitude": "latitude", "Longitude": "longitude"},
        date_col="event_date",
        lat_col="latitude",
        lon_col="longitude",
        zip_col="zip",
        borough_col="borough",
        filter_mode="active_aep",
    ),
    "location_pluto": DatasetSpec(
        name="location_pluto",
        raw_relpath="buildings/pluto.csv",
        output_dir="location",
        indexed_name="location_index.parquet",
        score_name="location_index.parquet",
        dataset_type="location_index",
        usecols=["address", "borough", "postcode", "BBL", "latitude", "longitude", "appbbl"],
        rename_map={},
        lat_col="latitude",
        lon_col="longitude",
        zip_col="postcode",
        borough_col="borough",
    ),
}


# ----------------------------------------------------------------------------
# Cleaning + loading
# ----------------------------------------------------------------------------

def _apply_filter(df: pd.DataFrame, spec: DatasetSpec) -> pd.DataFrame:
    mode = spec.filter_mode
    if mode == "rodent_positive":
        return df[df["RESULT"].fillna("").str.upper().str.contains("RAT|FAILED|ACTIVE", regex=True)]
    if mode == "safety_311":
        complaint_col = "Problem (formerly Complaint Type)"
        return df[
            df[complaint_col]
            .fillna("")
            .str.upper()
            .isin({"RODENT", "SANITATION CONDITION", "UNSANITARY CONDITION"})
        ]
    if mode == "open_violations":
        return df[~df["ViolationStatus"].fillna("").str.upper().str.contains("CLOSE")]
    if mode == "active_aep":
        return df[~df["CURRENT_STATUS"].fillna("").str.upper().str.contains("DISCHARG")]
    if mode == "alive_tree":
        return df[df["status"].fillna("").str.upper() == "ALIVE"]
    if mode == "live_linknyc":
        return df[df["Installation Status"].fillna("").str.upper() == "LIVE"]
    if mode == "operational_toilet":
        return df[df["Status"].fillna("").str.upper() == "OPERATIONAL"]
    if mode == "current_bike_route":
        return df[df["status"].fillna("").str.upper() == "CURRENT"]
    return df


def _prepare_dataframe(spec: DatasetSpec, raw_root: Path) -> pd.DataFrame:
    raw_path = raw_root / spec.raw_relpath
    df = pd.read_csv(raw_path, usecols=spec.usecols, low_memory=False)
    if spec.rename_map:
        df = df.rename(columns=spec.rename_map)
    df = _apply_filter(df, spec)
    if spec.lat_col and spec.lon_col:
        df[spec.lat_col] = pd.to_numeric(df[spec.lat_col], errors="coerce")
        df[spec.lon_col] = pd.to_numeric(df[spec.lon_col], errors="coerce")
        df = df[df[spec.lat_col].notna() & df[spec.lon_col].notna()].copy()
        # Drop rows outside NYC so downstream H3 encoding/aggregation doesn't
        # produce overview cells in Alabama or the middle of the Atlantic.
        df = df[
            df[spec.lat_col].between(NYC_BBOX["lat_min"], NYC_BBOX["lat_max"])
            & df[spec.lon_col].between(NYC_BBOX["lon_min"], NYC_BBOX["lon_max"])
        ].copy()
    if spec.date_col and spec.date_col in df.columns:
        df[spec.date_col] = safe_datetime(df[spec.date_col])
    if spec.zip_col and spec.zip_col in df.columns:
        df["zip"] = df[spec.zip_col].apply(normalize_zip)
    if spec.borough_col and spec.borough_col in df.columns:
        df["borough"] = df[spec.borough_col].apply(normalize_borough)
    if spec.lat_col == "latitude" and spec.lon_col == "longitude":
        df = add_h3(df, "latitude", "longitude")
    return df


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.unlink(missing_ok=True)
    compression_options: dict[str, Any] = {"compression": PARQUET_COMPRESSION}
    if PARQUET_COMPRESSION.lower() in {"zstd", "gzip", "brotli"}:
        compression_options["compression_level"] = PARQUET_COMPRESSION_LEVEL
    df.to_parquet(
        partial,
        index=False,
        engine="pyarrow",
        row_group_size=PARQUET_ROW_GROUP_ROWS,
        use_dictionary=True,
        write_statistics=True,
        **compression_options,
    )
    partial.replace(path)


# ----------------------------------------------------------------------------
# Processors
# ----------------------------------------------------------------------------

def _process_point_dataset(spec: DatasetSpec, raw_root: Path, ready_root: Path) -> None:
    df = _prepare_dataframe(spec, raw_root)
    _write_parquet(df, ready_root / spec.output_dir / spec.indexed_name)

    if spec.entity_col and spec.entity_col in df.columns:
        grouped = (
            df.groupby("h3_r9")[spec.entity_col]
            .nunique()
            .reset_index(name="raw_count")
        )
        inspection_count = df.groupby("h3_r9").size().reset_index(name="inspection_count")
        grouped = grouped.merge(inspection_count, on="h3_r9", how="left")
    else:
        grouped = df.groupby("h3_r9").size().reset_index(name="raw_count")

    if spec.critical_col and spec.critical_col in df.columns:
        critical_mask = df[spec.critical_col].fillna("").str.upper() == "CRITICAL"
        critical_by_cell = (
            df.assign(_crit=critical_mask.astype(int))
            .groupby("h3_r9")["_crit"]
            .sum()
            .reset_index(name="critical_count")
        )
        grouped = grouped.merge(critical_by_cell, on="h3_r9", how="left")
        grouped["critical_count"] = grouped["critical_count"].fillna(0).astype(int)
        denominator = (
            grouped["inspection_count"]
            if "inspection_count" in grouped.columns
            else grouped["raw_count"]
        )
        grouped["critical_rate"] = (grouped["critical_count"] / denominator.clip(lower=1)).round(3)
        # Abundance score (more restaurants = more access = good).
        abundance = percentile_score(grouped["raw_count"], access_mode=True)
        # Quality score: lower critical rate = better.  Invert so low crit
        # rate → high score.  This is an independent axis from abundance.
        quality = percentile_score(grouped["critical_rate"], access_mode=False)
        # Final = 50/50 blend so neither axis dominates.
        grouped["score"] = ((abundance + quality) / 2).round().clip(lower=0, upper=100).astype(int)
    else:
        grouped["score"] = percentile_score(grouped["raw_count"], spec.access_mode)

    _write_parquet(grouped, ready_root / spec.output_dir / spec.score_name)

    quarterly: pd.DataFrame | None = None
    if spec.trend_name and spec.date_col and spec.date_col in df.columns:
        trend_df = df[df[spec.date_col].notna()].copy()
        trend_df["quarter"] = quarter_label(trend_df[spec.date_col])
        quarterly = trend_df.groupby(["h3_r9", "quarter"]).size().reset_index(name="count")
        _write_parquet(quarterly, ready_root / spec.output_dir / spec.trend_name)

    for extra in spec.extra_outputs:
        if extra.get("kind") != "score_copy":
            continue
        target_dir = ready_root / extra["output_dir"]
        _write_parquet(df, target_dir / extra["indexed_name"])
        _write_parquet(grouped, target_dir / extra["score_name"])
        trend_path = extra.get("trend_name")
        if trend_path and quarterly is not None:
            _write_parquet(quarterly, target_dir / trend_path)


def _process_zip_response_dataset(spec: DatasetSpec, raw_root: Path, ready_root: Path) -> None:
    df = _prepare_dataframe(spec, raw_root)
    df[spec.value_col] = pd.to_numeric(df[spec.value_col], errors="coerce")
    df = df[df[spec.value_col].notna() & df["zip"].notna()].copy()
    grouped = df.groupby("zip").agg(
        avg_response_seconds=(spec.value_col, "mean"),
        incident_count=(spec.value_col, "count"),
    ).reset_index()
    grouped["score"] = response_time_score(grouped["avg_response_seconds"])
    _write_parquet(grouped, ready_root / spec.output_dir / spec.score_name)
    _write_parquet(df, ready_root / spec.output_dir / spec.indexed_name)


def _process_zip_sum_dataset(spec: DatasetSpec, raw_root: Path, ready_root: Path) -> None:
    df = _prepare_dataframe(spec, raw_root)
    df[spec.value_col] = pd.to_numeric(df[spec.value_col], errors="coerce").fillna(0)
    df = df[df["zip"].notna()].copy()
    grouped = df.groupby("zip").agg(
        total_value=(spec.value_col, "sum"),
        record_count=(spec.value_col, "count"),
    ).reset_index()
    # v3.7.8 fix: score from total_value (e.g. total park acreage), not record count.
    grouped["score"] = percentile_score(grouped["total_value"], access_mode=spec.access_mode)
    _write_parquet(grouped, ready_root / spec.output_dir / spec.score_name)
    _write_parquet(df, ready_root / spec.output_dir / spec.indexed_name)


def _process_location_index(spec: DatasetSpec, raw_root: Path, ready_root: Path) -> None:
    df = _prepare_dataframe(spec, raw_root)
    df["matched_address"] = df.get("address")
    df["canonical_location_id"] = df["BBL"].fillna(df.get("appbbl")).apply(
        lambda value: f"pluto_{int(value)}" if pd.notna(value) else None
    )
    cols = [
        col
        for col in [
            "matched_address",
            "borough",
            "zip",
            "latitude",
            "longitude",
            "canonical_location_id",
            "BBL",
            "appbbl",
        ]
        if col in df.columns
    ]
    _write_parquet(df[cols], ready_root / spec.output_dir / spec.indexed_name)


# --- WKT line-vertices processor (bike routes, open streets) ---------------

_WKT_COORD_RE = re.compile(r"(-?\d+\.\d+)\s+(-?\d+\.\d+)")


def _extract_wkt_vertices(wkt: Any) -> list[tuple[float, float]]:
    """Parse coordinate pairs out of a WKT MULTILINESTRING / LINESTRING.

    Returns a list of ``(lon, lat)`` tuples. We skip shapely/geopandas because
    the ready layer only needs per-cell counts.
    """
    if wkt is None or (isinstance(wkt, float) and pd.isna(wkt)):
        return []
    text = str(wkt)
    return [(float(m.group(1)), float(m.group(2))) for m in _WKT_COORD_RE.finditer(text)]


def _process_line_vertices_dataset(spec: DatasetSpec, raw_root: Path, ready_root: Path) -> None:
    raw_path = raw_root / spec.raw_relpath
    df = pd.read_csv(raw_path, usecols=spec.usecols, low_memory=False)
    df = _apply_filter(df, spec)

    wkt_col = spec.wkt_col or "the_geom"
    if wkt_col not in df.columns:
        raise ValueError(f"{spec.name}: expected WKT column {wkt_col!r} not found in CSV")

    rows: list[dict[str, Any]] = []
    meta_cols = [col for col in df.columns if col != wkt_col]
    for record_idx, row in enumerate(df.itertuples(index=False)):
        record = row._asdict()
        vertices = _extract_wkt_vertices(record.get(wkt_col))
        if not vertices:
            continue
        for lon, lat in vertices:
            rows.append({
                "record_index": record_idx,
                "latitude": lat,
                "longitude": lon,
                **{meta: record.get(meta) for meta in meta_cols},
            })

    if not rows:
        _write_parquet(pd.DataFrame(columns=["latitude", "longitude", "h3_r9"]), ready_root / spec.output_dir / spec.indexed_name)
        _write_parquet(pd.DataFrame(columns=["h3_r9", "raw_count", "score"]), ready_root / spec.output_dir / spec.score_name)
        return

    vertex_df = pd.DataFrame(rows)
    vertex_df = vertex_df[
        vertex_df["latitude"].between(NYC_BBOX["lat_min"], NYC_BBOX["lat_max"])
        & vertex_df["longitude"].between(NYC_BBOX["lon_min"], NYC_BBOX["lon_max"])
    ].copy()
    vertex_df = add_h3(vertex_df, "latitude", "longitude")
    _write_parquet(vertex_df, ready_root / spec.output_dir / spec.indexed_name)

    # Count distinct segments per H3 cell so thick linestrings (more vertices)
    # don't artificially inflate density.
    per_cell = (
        vertex_df.dropna(subset=["h3_r9"])
        .drop_duplicates(subset=["h3_r9", "record_index"])
        .groupby("h3_r9")
        .size()
        .reset_index(name="raw_count")
    )
    per_cell["score"] = percentile_score(per_cell["raw_count"], access_mode=True)
    _write_parquet(per_cell, ready_root / spec.output_dir / spec.score_name)


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------

def process_dataset(name: str, raw_root: Path | None = None, ready_root: Path | None = None) -> None:
    spec = SPECS[name]
    raw_root = raw_root or DEFAULT_RAW_ROOT
    ready_root = ready_root or DEFAULT_READY_ROOT

    if spec.dataset_type == "point_count":
        _process_point_dataset(spec, raw_root, ready_root)
    elif spec.dataset_type == "zip_response":
        _process_zip_response_dataset(spec, raw_root, ready_root)
    elif spec.dataset_type == "zip_sum":
        _process_zip_sum_dataset(spec, raw_root, ready_root)
    elif spec.dataset_type == "location_index":
        _process_location_index(spec, raw_root, ready_root)
    elif spec.dataset_type == "line_vertices":
        _process_line_vertices_dataset(spec, raw_root, ready_root)
    else:
        raise ValueError(f"Unsupported dataset_type for {name}: {spec.dataset_type}")


def rescore_from_indexed(name: str, ready_root: Path | None = None) -> bool:
    """Recompute ``*_scores_*.parquet`` from an existing indexed file.

    This is used by ``rescore_all`` to avoid re-parsing the raw NYC CSVs when
    the only thing that changed is the scoring formula. It keeps the scoring
    pipeline in sync with ``process_dataset`` by re-using the same helpers.

    Returns True when a rescore was actually performed, False when the
    indexed file is missing (caller should fall back to ``process_dataset``).
    """
    spec = SPECS[name]
    ready_root = ready_root or DEFAULT_READY_ROOT
    indexed_path = ready_root / spec.output_dir / spec.indexed_name
    if not indexed_path.exists():
        return False

    if spec.dataset_type == "point_count":
        df = pd.read_parquet(indexed_path)
        if "h3_r9" not in df.columns:
            return False
        if spec.entity_col and spec.entity_col in df.columns:
            grouped = (
                df.groupby("h3_r9")[spec.entity_col]
                .nunique()
                .reset_index(name="raw_count")
            )
            inspection_count = df.groupby("h3_r9").size().reset_index(name="inspection_count")
            grouped = grouped.merge(inspection_count, on="h3_r9", how="left")
        else:
            grouped = df.groupby("h3_r9").size().reset_index(name="raw_count")
        if spec.critical_col and spec.critical_col in df.columns:
            critical_mask = df[spec.critical_col].fillna("").str.upper() == "CRITICAL"
            critical_by_cell = (
                df.assign(_crit=critical_mask.astype(int))
                .groupby("h3_r9")["_crit"]
                .sum()
                .reset_index(name="critical_count")
            )
            grouped = grouped.merge(critical_by_cell, on="h3_r9", how="left")
            grouped["critical_count"] = grouped["critical_count"].fillna(0).astype(int)
            denominator = (
                grouped["inspection_count"]
                if "inspection_count" in grouped.columns
                else grouped["raw_count"]
            )
            grouped["critical_rate"] = (grouped["critical_count"] / denominator.clip(lower=1)).round(3)
            abundance = percentile_score(grouped["raw_count"], access_mode=True)
            quality = percentile_score(grouped["critical_rate"], access_mode=False)
            grouped["score"] = ((abundance + quality) / 2).round().clip(lower=0, upper=100).astype(int)
        else:
            grouped["score"] = percentile_score(grouped["raw_count"], spec.access_mode)
        _write_parquet(grouped, ready_root / spec.output_dir / spec.score_name)

        # Trend file is untouched (same counts, same time bins) - only the
        # score file needs regenerating. Rewrite only if the source trend is
        # missing for some reason.
        if spec.trend_name and spec.date_col and spec.date_col in df.columns:
            trend_path = ready_root / spec.output_dir / spec.trend_name
            if not trend_path.exists():
                trend_df = df[df[spec.date_col].notna()].copy()
                trend_df["quarter"] = quarter_label(trend_df[spec.date_col])
                quarterly = trend_df.groupby(["h3_r9", "quarter"]).size().reset_index(name="count")
                _write_parquet(quarterly, trend_path)

        for extra in spec.extra_outputs:
            if extra.get("kind") != "score_copy":
                continue
            target_dir = ready_root / extra["output_dir"]
            _write_parquet(df, target_dir / extra["indexed_name"])
            _write_parquet(grouped, target_dir / extra["score_name"])
            trend_path = extra.get("trend_name")
            if trend_path and spec.date_col and spec.date_col in df.columns:
                trend_df = df[df[spec.date_col].notna()].copy()
                trend_df["quarter"] = quarter_label(trend_df[spec.date_col])
                quarterly = trend_df.groupby(["h3_r9", "quarter"]).size().reset_index(name="count")
                _write_parquet(quarterly, target_dir / trend_path)
        return True

    if spec.dataset_type == "zip_response":
        df = pd.read_parquet(indexed_path)
        if spec.value_col not in df.columns:
            return False
        grouped = df.groupby("zip").agg(
            avg_response_seconds=(spec.value_col, "mean"),
            incident_count=(spec.value_col, "count"),
        ).reset_index()
        grouped["score"] = response_time_score(grouped["avg_response_seconds"])
        _write_parquet(grouped, ready_root / spec.output_dir / spec.score_name)
        return True

    if spec.dataset_type == "zip_sum":
        df = pd.read_parquet(indexed_path)
        if spec.value_col not in df.columns:
            return False
        df[spec.value_col] = pd.to_numeric(df[spec.value_col], errors="coerce").fillna(0)
        grouped = df.groupby("zip").agg(
            total_value=(spec.value_col, "sum"),
            record_count=(spec.value_col, "count"),
        ).reset_index()
        grouped["score"] = percentile_score(grouped["total_value"], access_mode=spec.access_mode)
        _write_parquet(grouped, ready_root / spec.output_dir / spec.score_name)
        return True

    # line_vertices / location_index cannot be rescored without the raw CSV.
    return False


def rescore_all(ready_root: Path | None = None, skip: set[str] | None = None) -> list[str]:
    ready_root = ready_root or DEFAULT_READY_ROOT
    skip = skip or set()
    done: list[str] = []
    for name, spec in SPECS.items():
        if name in skip or spec.dataset_type in {"location_index", "line_vertices"}:
            continue
        try:
            if rescore_from_indexed(name, ready_root=ready_root):
                done.append(name)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[rescore] {name} failed: {exc}")
    return done


def build_baselines(ready_root: Path | None = None) -> None:
    ready_root = ready_root or DEFAULT_READY_ROOT
    baseline_payload: dict[str, Any] = {}
    score_files: list[Path] = []
    for pattern in ("**/*_scores_h3.parquet", "**/*_scores_zip.parquet"):
        score_files.extend(sorted(ready_root.glob(pattern)))
    seen: set[Path] = set()
    for path in score_files:
        if path in seen:
            continue
        seen.add(path)
        df = pd.read_parquet(path)
        preferred_order = ["raw_count", "avg_response_seconds", "total_value", "score"]
        metric_col = next((col for col in preferred_order if col in df.columns), None)
        if metric_col is None:
            continue
        series = pd.to_numeric(df[metric_col], errors="coerce").dropna()
        if series.empty:
            continue
        baseline_payload[path.stem] = {
            "metric": metric_col,
            "p25": round(float(series.quantile(0.25)), 2),
            "p50": round(float(series.quantile(0.50)), 2),
            "p75": round(float(series.quantile(0.75)), 2),
        }
    output_path = ready_root / "baselines" / "baselines.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(baseline_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", choices=sorted(SPECS.keys()) + ["baselines", "rescore_all"])
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--ready-root", type=Path, default=DEFAULT_READY_ROOT)
    args = parser.parse_args()
    if args.dataset == "baselines":
        build_baselines(args.ready_root)
        return
    if args.dataset == "rescore_all":
        done = rescore_all(ready_root=args.ready_root)
        print(f"[rescore_all] rescored {len(done)} datasets: {', '.join(done)}")
        return
    process_dataset(args.dataset, raw_root=args.raw_root, ready_root=args.ready_root)


if __name__ == "__main__":
    main()
