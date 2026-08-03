#!/usr/bin/env python3
"""Audit the raw Urban Dossier CSV snapshot without modifying source data."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class DatasetExpectation:
    path: str
    required_columns: tuple[str, ...]
    expected_rows: int | None = None


DATASETS = (
    DatasetExpectation("safety/motor_vehicle_collisions.csv", ("CRASH DATE", "LATITUDE", "LONGITUDE", "ZIP CODE", "BOROUGH", "COLLISION_ID")),
    DatasetExpectation("safety/ems_incident_dispatch.csv", ("ZIPCODE", "INCIDENT_RESPONSE_SECONDS_QY", "BOROUGH")),
    DatasetExpectation("safety/fire_incident_dispatch.csv", ("ZIPCODE", "INCIDENT_RESPONSE_SECONDS_QY", "INCIDENT_BOROUGH")),
    DatasetExpectation("environment/rodent_inspections.csv", ("INSPECTION_DATE", "RESULT", "LATITUDE", "LONGITUDE", "ZIP_CODE", "BOROUGH", "JOB_ID")),
    DatasetExpectation("quality_of_life/311_service_requests_2020_present.csv", ("Unique Key", "Created Date", "Problem (formerly Complaint Type)", "Latitude", "Longitude", "Incident Zip", "Borough")),
    DatasetExpectation("transit/mta_subway_entrances_exits_2024.csv", ("Stop Name", "Entrance Latitude", "Entrance Longitude", "Borough"), 2120),
    DatasetExpectation("transit/bus_stop_shelters.csv", ("Shelter_ID", "On_Street", "Cross_Stre", "Latitude", "Longitude", "BoroName"), 3381),
    DatasetExpectation("transit/nyc_bike_routes.csv", ("the_geom", "segmentid", "street", "boro", "allclasses"), 29695),
    DatasetExpectation("transit/open_streets_locations.csv", ("Object ID", "Organization Name", "Approved On Street", "Borough Name", "The_Geom")),
    DatasetExpectation("amenities/dohmh_restaurant_inspections.csv", ("CAMIS", "DBA", "INSPECTION DATE", "CRITICAL FLAG", "Latitude", "Longitude", "ZIPCODE", "BORO")),
    DatasetExpectation("amenities/parks_properties.csv", ("NAME311", "ACRES", "ZIPCODE", "BOROUGH", "WATERFRONT"), 2059),
    DatasetExpectation("amenities/street_trees.csv", ("tree_id", "created_at", "latitude", "longitude", "postcode", "borough", "status", "health")),
    DatasetExpectation("amenities/linknyc_kiosk_locations.csv", ("Site ID", "Street Address", "Installation Status", "Latitude", "Longitude", "Postcode", "Borough")),
    DatasetExpectation("amenities/public_toilets.csv", ("Facility Name", "Status", "Latitude", "Longitude"), 1066),
    DatasetExpectation("amenities/facilities_database.csv", ("uid", "facname", "facgroup", "facsubgrp", "zipcode", "boro", "latitude", "longitude")),
    DatasetExpectation("buildings/housing_code_violations.csv", ("ViolationID", "Class", "InspectionDate", "CurrentStatus", "ViolationStatus", "Latitude", "Longitude", "Postcode", "Borough", "BBL", "BIN")),
    DatasetExpectation("buildings/buildings_aep.csv", ("BUILDING_ID", "CURRENT_STATUS", "AEP_START_DATE", "Postcode", "BOROUGH", "Latitude", "Longitude", "BBL", "BIN")),
    DatasetExpectation("buildings/pluto.csv", ("address", "borough", "postcode", "BBL", "latitude", "longitude", "appbbl")),
)


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def audit(raw_root: Path) -> dict[str, object]:
    connection = duckdb.connect()
    results: list[dict[str, object]] = []

    for expected in DATASETS:
        source = raw_root / expected.path
        result: dict[str, object] = {
            "path": expected.path,
            "exists": source.is_file(),
            "expected_rows": expected.expected_rows,
        }
        if not source.is_file():
            result.update(status="missing", errors=["file is missing"])
            results.append(result)
            continue

        result["size_bytes"] = source.stat().st_size
        try:
            columns = read_header(source)
            missing_columns = [name for name in expected.required_columns if name not in columns]
            duplicate_columns = sorted({name for name in columns if columns.count(name) > 1})
            result.update(
                columns=columns,
                column_count=len(columns),
                missing_required_columns=missing_columns,
                duplicate_columns=duplicate_columns,
            )
        except Exception as exc:  # noqa: BLE001
            result.update(status="invalid", errors=[f"header: {type(exc).__name__}: {exc}"])
            results.append(result)
            continue

        try:
            row_count = connection.execute(
                """
                SELECT count(*)
                FROM read_csv(
                    ?,
                    header = true,
                    all_varchar = true,
                    strict_mode = true,
                    sample_size = 20000
                )
                """,
                [str(source)],
            ).fetchone()[0]
            result["row_count"] = row_count
            result["row_count_matches_expected"] = (
                expected.expected_rows is None or row_count == expected.expected_rows
            )
            errors: list[str] = []
            if missing_columns:
                errors.append(f"missing required columns: {', '.join(missing_columns)}")
            if duplicate_columns:
                errors.append(f"duplicate columns: {', '.join(duplicate_columns)}")
            if row_count == 0:
                errors.append("dataset contains no rows")
            if expected.expected_rows is not None and row_count != expected.expected_rows:
                errors.append(f"expected {expected.expected_rows} rows, found {row_count}")
            result["errors"] = errors
            result["status"] = "ok" if not errors else "incompatible"
        except Exception as exc:  # noqa: BLE001
            result.update(
                status="invalid",
                errors=[f"full parse: {type(exc).__name__}: {exc}"],
            )
        results.append(result)

    expected_paths = {item.path for item in DATASETS}
    actual_paths = {
        str(path.relative_to(raw_root)) for path in raw_root.glob("*/*.csv")
    }
    counts = {
        status: sum(item.get("status") == status for item in results)
        for status in ("ok", "incompatible", "invalid", "missing")
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "raw_root": str(raw_root.resolve()),
        "expected_dataset_count": len(DATASETS),
        "actual_csv_count": len(actual_paths),
        "unexpected_csv_files": sorted(actual_paths - expected_paths),
        "missing_csv_files": sorted(expected_paths - actual_paths),
        "status_counts": counts,
        "datasets": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = audit(args.raw_root)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
