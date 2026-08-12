"""Publish the official NYC Heat Vulnerability Index at its native ZCTA grain.

HVI is an ordinal 1--5 quintile supplied by NYC DOHMH, not a continuous
temperature measurement. The publication preserves the source geography and
maps the five official risk classes to a display score only:

    score = (5 - hvi) * 25

No ZCTA-to-NTA or ZCTA-to-H3 allocation is performed. The raw CSV and saved
official Socrata metadata are hash-pinned, and the ready parquet/manifest pair
is fail-closed under the current methodology version.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import date
from pathlib import Path

import duckdb

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from urban_dossier_backend.metrics import METHODOLOGY_VERSION


SOURCE_CSV = "hvi_4mhf-duep.csv"
SOURCE_METADATA = "4mhf-duep.json"
SOURCE_CSV_SHA256 = "f6d545a38fe19aff8a3d722d71568bb380ceb40ecf9bea1cd2037a4ed3e91166"
SOURCE_METADATA_SHA256 = "5e84a417b0541638a2c87fe84118642035d3c68f456f925ab0581c7bb2961a38"
OUTPUT_RELPATH = Path("environment/hvi_scores_zip.parquet")
MANIFEST_RELPATH = Path("environment/hvi.manifest.json")
ARTIFACT_COLUMNS = ["zip", "raw_count", "score"]
EXPECTED_DISTRIBUTION = {1: 37, 2: 37, 3: 36, 4: 37, 5: 37}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_snapshot(raw_dir: Path, metadata_path: Path) -> dict:
    manifest_path = raw_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {
        entry["file"]: entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("file"), str)
    }
    source_path = raw_dir / SOURCE_CSV
    stamp = declared.get(SOURCE_CSV) or {}
    if not source_path.is_file():
        raise ValueError(f"HVI source is missing: {SOURCE_CSV}")
    if (
        stamp.get("bytes") != source_path.stat().st_size
        or stamp.get("sha256") != SOURCE_CSV_SHA256
        or sha256(source_path) != SOURCE_CSV_SHA256
    ):
        raise ValueError("HVI source does not match its pinned manifest")
    if not metadata_path.is_file() or sha256(metadata_path) != SOURCE_METADATA_SHA256:
        raise ValueError("HVI official metadata snapshot does not match its pinned hash")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    fields = {column.get("fieldName") for column in metadata.get("columns", [])}
    if metadata.get("id") != "4mhf-duep" or fields != {"zcta20", "hvi"}:
        raise ValueError("HVI metadata id or schema is not the expected official asset")
    return {
        "manifest": {"path": str(manifest_path), "sha256": sha256(manifest_path)},
        "csv": {
            "path": str(source_path),
            "sha256": SOURCE_CSV_SHA256,
            "size_bytes": source_path.stat().st_size,
        },
        "metadata": {
            "path": str(metadata_path),
            "sha256": SOURCE_METADATA_SHA256,
            "size_bytes": metadata_path.stat().st_size,
        },
    }


def build_rows(raw_dir: Path, metadata_path: Path) -> tuple[list[tuple[str, int, int]], dict]:
    inputs = validate_snapshot(raw_dir, metadata_path)
    source_path = raw_dir / SOURCE_CSV
    rows: list[tuple[str, int, int]] = []
    distribution = {rank: 0 for rank in range(1, 6)}
    with source_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["zcta20", "hvi"]:
            raise ValueError(f"HVI CSV schema changed: {reader.fieldnames}")
        for source in reader:
            zcta = (source.get("zcta20") or "").strip()
            if len(zcta) != 5 or not zcta.isdigit():
                raise ValueError(f"HVI has an invalid ZCTA: {zcta!r}")
            try:
                rank = int(source.get("hvi") or "")
            except ValueError as exc:
                raise ValueError(f"HVI has a non-integer rank for {zcta}") from exc
            if rank not in distribution:
                raise ValueError(f"HVI rank for {zcta} is outside 1--5: {rank}")
            distribution[rank] += 1
            rows.append((zcta, rank, (5 - rank) * 25))

    rows.sort()
    if len(rows) != 184 or len({row[0] for row in rows}) != len(rows):
        raise ValueError("HVI snapshot must contain 184 unique ZCTAs")
    if distribution != EXPECTED_DISTRIBUTION:
        raise ValueError(f"HVI quintile distribution changed: {distribution}")
    return rows, {
        "inputs": inputs,
        "source": {
            "dataset_id": "4mhf-duep",
            "publisher": "NYC Department of Health and Mental Hygiene",
            "geography": "2020 ZIP Code Tabulation Area (ZCTA)",
            "published": "2024-09-19",
            "update_policy": "as needed; component datasets refresh every 3--5 years",
            "component_vintages": {
                "income": "ACS 5-year 2016-2020",
                "vegetative_cover": "2017 LiDAR",
                "population": "2020 Census",
                "surface_temperature": "ECOSTRESS 2020-08-27",
                "air_conditioning": "2017 Housing and Vacancy Survey",
            },
        },
        "population": {
            "zctas": len(rows),
            "missing": 0,
            "hvi_distribution": {str(rank): count for rank, count in distribution.items()},
        },
        "normalization": {
            "method": "official ordinal reversal",
            "formula": "score = (5 - hvi) * 25",
            "mapping": {"1": 100, "2": 75, "3": 50, "4": 25, "5": 0},
        },
    }


def publish(rows: list[tuple[str, int, int]], build: dict, ready_root: Path) -> dict:
    artifact_path = ready_root / OUTPUT_RELPATH
    manifest_path = ready_root / MANIFEST_RELPATH
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    temp_artifact = artifact_path.with_name(f".{artifact_path.name}.{os.getpid()}.tmp")
    temp_manifest = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE artifact (zip VARCHAR, raw_count INTEGER, score INTEGER)")
        con.executemany("INSERT INTO artifact VALUES (?, ?, ?)", rows)
        con.execute(
            f"COPY artifact TO '{temp_artifact.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        columns = [
            entry[0]
            for entry in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{temp_artifact.as_posix()}')"
            ).fetchall()
        ]
        row_count, unique_zctas, min_raw, max_raw, min_score, max_score = con.execute(
            f"SELECT count(*), count(DISTINCT zip), min(raw_count), max(raw_count), "
            f"min(score), max(score) FROM read_parquet('{temp_artifact.as_posix()}')"
        ).fetchone()
        if columns != ARTIFACT_COLUMNS or row_count != len(rows) or unique_zctas != row_count:
            raise ValueError("HVI output schema, row count or ZCTA uniqueness check failed")
        if (min_raw, max_raw, min_score, max_score) != (1, 5, 0, 100):
            raise ValueError("HVI publication falls outside the official ordinal mapping")
        manifest = {
            "schema_version": "1.0",
            "methodology_version": METHODOLOGY_VERSION,
            "generated": date.today().isoformat(),
            "source_dataset_id": "4mhf-duep",
            "artifact": {
                "path": OUTPUT_RELPATH.name,
                "sha256": sha256(temp_artifact),
                "size_bytes": temp_artifact.stat().st_size,
                "row_count": row_count,
                "columns": columns,
            },
            **build,
            "limitations": [
                "Ordinal relative-risk quintile, not temperature or an individual health prediction.",
                "ZCTA values are looked up by ZIP only; no NTA or H3 downscaling is performed.",
                "A low-vulnerability area still contains residents at risk during extreme heat.",
                "The index includes social and environmental factors and must not be labeled pure heat exposure.",
            ],
        }
        temp_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temp_artifact.replace(artifact_path)
        temp_manifest.replace(manifest_path)
        return manifest
    finally:
        con.close()
        temp_artifact.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("/mnt/data/urban-dossier-state/datasets/raw-expansion/hvi"),
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path("/mnt/data/urban-dossier-state/datasets/raw-expansion/_meta/4mhf-duep.json"),
    )
    parser.add_argument("--ready-root", type=Path, default=Path("data/ready"))
    args = parser.parse_args()
    rows, build = build_rows(args.raw_dir, args.metadata)
    print(json.dumps(publish(rows, build, args.ready_root), indent=2))


if __name__ == "__main__":
    main()
