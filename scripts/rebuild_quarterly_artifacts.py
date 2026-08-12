#!/usr/bin/env python3
"""Atomically rebuild period-clean quarterly Gold artifacts from indexed data."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import duckdb


DATASETS = {
    "restaurants": (
        "amenities/restaurants_indexed.parquet",
        "amenities/restaurants_quarterly_h3.parquet",
    ),
    "housing_violations": (
        "building/housing_violations_indexed.parquet",
        "building/housing_violations_quarterly_h3.parquet",
    ),
}


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def rebuild(ready_root: Path, names: list[str], backup_dir: Path) -> list[dict]:
    connection = duckdb.connect()
    backup_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        if name not in DATASETS:
            raise ValueError(f"dataset must be one of {', '.join(DATASETS)}")
        indexed_relative, quarterly_relative = DATASETS[name]
        indexed = ready_root / indexed_relative
        target = ready_root / quarterly_relative
        if not indexed.exists() or not target.exists():
            raise FileNotFoundError(f"missing indexed or quarterly artifact for {name}")
        backup = backup_dir / f"{name}-quarterly.parquet"
        if backup.exists():
            raise FileExistsError(f"refusing to overwrite recovery backup: {backup}")
        shutil.copy2(target, backup)
        partial = target.with_suffix(target.suffix + ".part")
        partial.unlink(missing_ok=True)
        connection.execute(
            f"""
            COPY (
                SELECT
                    h3_r9,
                    cast(year(try_cast(event_date AS DATE)) AS VARCHAR)
                        || 'Q'
                        || cast(quarter(try_cast(event_date AS DATE)) AS VARCHAR) AS quarter,
                    count(*) AS count
                FROM read_parquet('{_quote(indexed)}')
                WHERE h3_r9 IS NOT NULL
                  AND try_cast(event_date AS DATE) BETWEEN DATE '2000-01-01' AND current_date
                GROUP BY h3_r9, quarter
            ) TO '{_quote(partial)}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 250000)
            """
        )
        rows, period_min, period_max, invalid = connection.execute(
            f"""
            SELECT count(*), min(quarter), max(quarter), count(*) FILTER (
                WHERE NOT regexp_full_match(quarter, '[0-9]{{4}}Q[1-4]')
                   OR cast(substr(quarter, 1, 4) AS INTEGER) < 2000
                   OR quarter > cast(year(current_date) AS VARCHAR)
                       || 'Q' || cast(quarter(current_date) AS VARCHAR)
            )
            FROM read_parquet('{_quote(partial)}')
            """
        ).fetchone()
        if invalid or not rows:
            raise RuntimeError(f"rebuilt {name} artifact failed period validation")
        partial.replace(target)
        results.append(
            {
                "dataset": name,
                "rows": rows,
                "period_min": period_min,
                "period_max": period_max,
                "invalid_period_rows": invalid,
                "backup": str(backup),
                "target": str(target),
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ready_root", type=Path)
    parser.add_argument("--datasets", nargs="+", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            rebuild(args.ready_root, args.datasets, args.backup_dir),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
