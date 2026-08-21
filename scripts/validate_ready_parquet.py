#!/usr/bin/env python3
"""Validate the complete Urban Dossier ready-layer Parquet publication."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq


EXPECTED_FILES = {
    "amenities/facilities_indexed.parquet",
    "amenities/facilities_scores_h3.parquet",
    "amenities/linknyc_indexed.parquet",
    "amenities/linknyc_scores_h3.parquet",
    "amenities/parks_indexed.parquet",
    "amenities/parks_scores_zip.parquet",
    "amenities/restaurants_indexed.parquet",
    "amenities/restaurants_quarterly_h3.parquet",
    "amenities/restaurants_scores_h3.parquet",
    "amenities/toilets_indexed.parquet",
    "amenities/toilets_scores_h3.parquet",
    "amenities/trees_indexed.parquet",
    "amenities/trees_quarterly_h3.parquet",
    "amenities/trees_scores_h3.parquet",
    "building/aep_indexed.parquet",
    "building/aep_scores_h3.parquet",
    "building/housing_violations_indexed.parquet",
    "building/housing_violations_quarterly_h3.parquet",
    "building/housing_violations_scores_h3.parquet",
    "context/population_r9.parquet",
    "context/population_r9_provenance.parquet",
    "environment/hvi_scores_zip.parquet",
    "environment/nyccas_no_scores_h3.parquet",
    "location/location_index.parquet",
    "analysis/sensitivity_cells.parquet",
    "safety/311_quarterly_h3.parquet",
    "safety/311_safety_indexed.parquet",
    "safety/311_scores_h3.parquet",
    "safety/collisions_indexed.parquet",
    "safety/collisions_quarterly_h3.parquet",
    "safety/collisions_scores_h3.parquet",
    "safety/ems_indexed.parquet",
    "safety/ems_scores_zip.parquet",
    "safety/fire_indexed.parquet",
    "safety/fire_scores_zip.parquet",
    "safety/rodent_indexed.parquet",
    "safety/rodent_quarterly_h3.parquet",
    "safety/rodent_rate_scores_h3.parquet",
    "transit/bike_routes_indexed.parquet",
    "transit/bike_routes_scores_h3.parquet",
    "transit/bus_indexed.parquet",
    "transit/bus_scores_h3.parquet",
    "transit/open_streets_indexed.parquet",
    "transit/open_streets_scores_h3.parquet",
    "transit/subway_indexed.parquet",
    "transit/subway_scores_h3.parquet",
}

# Valid ready-layer artifacts that are deliberately outside the published
# scoring registry. They are audited like every other Parquet file but do not
# make a clean publication fail the exact active-file contract.
ALLOWED_AUXILIARY_FILES = {
    "transit/transit_risk_scores_h3.parquet",
}


def quote_path(path: Path) -> str:
    return str(path).replace("'", "''")


def validate(root: Path, compression: str, max_row_group_rows: int) -> dict[str, object]:
    connection = duckdb.connect()
    actual = {str(path.relative_to(root)) for path in root.rglob("*.parquet")}
    partials = sorted(str(path.relative_to(root)) for path in root.rglob("*.part"))
    files: list[dict[str, object]] = []

    for relative in sorted(actual):
        path = root / relative
        errors: list[str] = []
        parquet = pq.ParquetFile(path)
        metadata = parquet.metadata
        columns = set(parquet.schema_arrow.names)
        period_min = period_max = None

        for group_index in range(metadata.num_row_groups):
            group = metadata.row_group(group_index)
            if group.num_rows > max_row_group_rows:
                errors.append(
                    f"row group {group_index} has {group.num_rows} rows; max is {max_row_group_rows}"
                )
            for column_index in range(group.num_columns):
                codec = group.column(column_index).compression.lower()
                if codec != compression.lower():
                    errors.append(
                        f"row group {group_index} column {column_index} uses {codec}, expected {compression}"
                    )

        escaped = quote_path(path)
        if "score" in columns:
            minimum, maximum, invalid = connection.execute(
                f"SELECT min(score), max(score), count(*) FILTER (WHERE score IS NULL OR score < 0 OR score > 100) "
                f"FROM read_parquet('{escaped}')"
            ).fetchone()
            if invalid:
                errors.append(f"{invalid} score values are null or outside 0..100")
        else:
            minimum = maximum = None

        if {"latitude", "longitude"}.issubset(columns):
            outside, null_coords = connection.execute(
                f"SELECT count(*) FILTER (WHERE latitude NOT BETWEEN 40.45 AND 40.95 "
                f"OR longitude NOT BETWEEN -74.30 AND -73.65), "
                f"count(*) FILTER (WHERE latitude IS NULL OR longitude IS NULL) "
                f"FROM read_parquet('{escaped}')"
            ).fetchone()
            if outside:
                errors.append(f"{outside} coordinate rows are outside the configured NYC bounding box")
            if null_coords:
                errors.append(f"{null_coords} coordinate rows are null")

        if "h3_r9" in columns:
            null_h3 = connection.execute(
                f"SELECT count(*) FROM read_parquet('{escaped}') WHERE h3_r9 IS NULL OR h3_r9 = ''"
            ).fetchone()[0]
            if null_h3:
                errors.append(f"{null_h3} h3_r9 values are null or empty")

        if "quarter" in columns:
            now = datetime.now(timezone.utc)
            current_quarter = f"{now.year:04d}Q{((now.month - 1) // 3) + 1}"
            period_min, period_max, invalid_periods = connection.execute(
                f"SELECT min(quarter), max(quarter), count(*) FILTER ("
                f"WHERE quarter IS NULL "
                f"OR NOT regexp_full_match(cast(quarter AS VARCHAR), '[0-9]{{4}}Q[1-4]') "
                f"OR try_cast(substr(cast(quarter AS VARCHAR), 1, 4) AS INTEGER) < 2000 "
                f"OR cast(quarter AS VARCHAR) > '{current_quarter}') "
                f"FROM read_parquet('{escaped}')"
            ).fetchone()
            if invalid_periods:
                errors.append(
                    f"{invalid_periods} quarter rows are malformed, pre-2000, or future-dated"
                )

        files.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "rows": metadata.num_rows,
                "columns": parquet.schema_arrow.names,
                "row_groups": metadata.num_row_groups,
                "score_min": minimum,
                "score_max": maximum,
                "period_min": period_min,
                "period_max": period_max,
                "status": "ok" if not errors else "invalid",
                "errors": errors,
            }
        )

    missing = sorted(EXPECTED_FILES - actual)
    unexpected = sorted(actual - EXPECTED_FILES - ALLOWED_AUXILIARY_FILES)
    auxiliary = sorted(actual & ALLOWED_AUXILIARY_FILES)
    invalid_count = sum(item["status"] != "ok" for item in files)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ready_root": str(root.resolve()),
        "expected_file_count": len(EXPECTED_FILES),
        "actual_file_count": len(actual),
        "missing_files": missing,
        "unexpected_files": unexpected,
        "auxiliary_files": auxiliary,
        "partial_files": partials,
        "status": "ok" if not missing and not unexpected and not partials and invalid_count == 0 else "invalid",
        "invalid_file_count": invalid_count,
        "total_rows_across_files": sum(int(item["rows"]) for item in files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ready_root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--max-row-group-rows", type=int, default=250_000)
    args = parser.parse_args()

    report = validate(args.ready_root, args.compression, args.max_row_group_rows)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
