"""Publish the latest NYCCAS nitric-oxide model surface at H3 r9.

The source is a complete City Open Data blob containing ESRI grids for sixteen
NYCCAS survey years. Year 16 uniquely includes a standard float32 GeoTIFF, so
this pipeline can read the official raster without adding a GDAL runtime. It
samples the raster value at every H3 r9 centroid inside the authoritative 2020
NTA land polygons. This is nearest-pixel lookup on the native model surface,
not interpolation and not a claim of finer observational precision.

The task fails before publication unless raw snapshot hashes, raster metadata,
coverage, value range and output schema all validate. The parquet and manifest
are written via temporary files and atomically replaced only after validation.

Usage:
    python backend/scripts/preprocess_nyccas_no.py \
      --raw-dir /mnt/data/urban-dossier-state/datasets/raw-expansion/nyccas \
      --boundary data/boundaries/nta_2020.geojson \
      --ready-root data/ready
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import duckdb
import h3
import numpy as np
from PIL import Image
from pyproj import Transformer

from preprocess_common import percentile_score
from urban_dossier_backend.metrics import METHODOLOGY_VERSION


SOURCE_ARCHIVE = "AnnAvg_1_16_300m.zip"
SOURCE_DICTIONARY = "Data-Dictionary_NYCCAS_March_2026.xlsx"
RASTER_MEMBER = "aa16_no300m/noAA16300m.tif"
OUTPUT_RELPATH = Path("environment/nyccas_no_scores_h3.parquet")
MANIFEST_RELPATH = Path("environment/nyccas_no.manifest.json")
ARTIFACT_COLUMNS = ["h3_r9", "raw_count", "score"]
SOURCE_CRS = "EPSG:2263"
SOURCE_PERIOD = ["2023-12", "2024-12"]
MIN_COVERAGE = 0.99
PLAUSIBLE_RANGE_PPB = (0.0, 200.0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_raw_snapshot(raw_dir: Path) -> dict:
    manifest_path = raw_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = {
        entry["file"]: entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("file"), str)
    }
    for filename in (SOURCE_ARCHIVE, SOURCE_DICTIONARY):
        path = raw_dir / filename
        stamp = declared.get(filename) or {}
        if not path.is_file():
            raise ValueError(f"NYCCAS source is missing: {filename}")
        if stamp.get("bytes") != path.stat().st_size or stamp.get("sha256") != sha256(path):
            raise ValueError(f"NYCCAS source does not match manifest: {filename}")
    return {
        "manifest": {
            "path": str(manifest_path),
            "sha256": sha256(manifest_path),
        },
        "archive": {
            "path": str(raw_dir / SOURCE_ARCHIVE),
            "sha256": declared[SOURCE_ARCHIVE]["sha256"],
            "size_bytes": declared[SOURCE_ARCHIVE]["bytes"],
        },
        "data_dictionary": {
            "path": str(raw_dir / SOURCE_DICTIONARY),
            "sha256": declared[SOURCE_DICTIONARY]["sha256"],
            "size_bytes": declared[SOURCE_DICTIONARY]["bytes"],
        },
    }


def land_cells(boundary_path: Path, resolution: int = 9) -> list[str]:
    payload = json.loads(boundary_path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection" or not payload.get("features"):
        raise ValueError("NTA boundary must be a non-empty FeatureCollection")
    cells: set[str] = set()
    for feature in payload["features"]:
        geometry = feature.get("geometry")
        if geometry:
            cells.update(h3.geo_to_cells(geometry, resolution))
    if not cells:
        raise ValueError("NTA boundary produced no H3 land cells")
    return sorted(cells)


def raster_metadata(image: Image.Image) -> dict:
    tags = image.tag_v2
    scale = tags.get(33550)
    tiepoint = tags.get(33922)
    nodata_value = tags.get(42113)
    if not scale or len(scale) < 2 or not tiepoint or len(tiepoint) < 6:
        raise ValueError("NYCCAS GeoTIFF is missing pixel scale or tiepoint tags")
    if nodata_value is None:
        raise ValueError("NYCCAS GeoTIFF is missing an explicit nodata tag")
    return {
        "width": image.width,
        "height": image.height,
        "x_origin": float(tiepoint[3]),
        "y_origin": float(tiepoint[4]),
        "pixel_width": float(scale[0]),
        "pixel_height": float(scale[1]),
        "nodata": float(nodata_value),
    }


def sample_cells(
    values: np.ndarray,
    cells: list[str],
    metadata: dict,
    transformer: Transformer,
) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for cell in cells:
        latitude, longitude = h3.cell_to_latlng(cell)
        x, y = transformer.transform(longitude, latitude)
        column = math.floor((x - metadata["x_origin"]) / metadata["pixel_width"])
        row = math.floor((metadata["y_origin"] - y) / metadata["pixel_height"])
        if not (0 <= row < metadata["height"] and 0 <= column < metadata["width"]):
            continue
        value = float(values[row, column])
        if not math.isfinite(value) or value == metadata["nodata"]:
            continue
        rows.append((cell, value))
    return rows


def build_rows(raw_dir: Path, boundary_path: Path) -> tuple[list[tuple], dict]:
    inputs = validate_raw_snapshot(raw_dir)
    cells = land_cells(boundary_path)
    with tempfile.TemporaryDirectory(prefix="urban-dossier-nyccas-") as directory:
        raster_path = Path(directory) / "nyccas_no_year16.tif"
        with ZipFile(raw_dir / SOURCE_ARCHIVE) as archive:
            try:
                member = archive.getinfo(RASTER_MEMBER)
            except KeyError as exc:
                raise ValueError(f"NYCCAS archive lacks {RASTER_MEMBER}") from exc
            with archive.open(member) as source, raster_path.open("wb") as target:
                shutil.copyfileobj(source, target)
        with Image.open(raster_path) as image:
            if image.mode != "F":
                raise ValueError(f"NYCCAS raster must be float32, got {image.mode}")
            metadata = raster_metadata(image)
            values = np.asarray(image, dtype=np.float64)

    sampled = sample_cells(
        values,
        cells,
        metadata,
        Transformer.from_crs("EPSG:4326", SOURCE_CRS, always_xy=True),
    )
    coverage = len(sampled) / len(cells)
    if coverage < MIN_COVERAGE:
        raise ValueError(f"NYCCAS land-cell coverage {coverage:.4f} is below {MIN_COVERAGE}")
    raw = np.array([value for _, value in sampled], dtype=float)
    if raw.size == 0 or raw.min() <= PLAUSIBLE_RANGE_PPB[0] or raw.max() > PLAUSIBLE_RANGE_PPB[1]:
        raise ValueError("NYCCAS nitric-oxide values fall outside the plausible ppb range")

    import pandas as pd

    frame = pd.DataFrame(sampled, columns=["h3_r9", "raw_count"])
    frame["score"] = percentile_score(frame["raw_count"], access_mode=False)
    frame = frame.sort_values("h3_r9").reset_index(drop=True)
    rows = list(frame.itertuples(index=False, name=None))
    build = {
        "inputs": inputs,
        "boundary": {
            "path": str(boundary_path),
            "sha256": sha256(boundary_path),
            "features": 262,
        },
        "source_raster": {
            "archive_member": RASTER_MEMBER,
            "survey_period": SOURCE_PERIOD,
            "pollutant": "nitric oxide (NO)",
            "unit": "ppb",
            "model": "NYCCAS land-use regression annual-average predicted surface",
            "crs": SOURCE_CRS,
            **metadata,
        },
        "population": {
            "definition": "H3 r9 centroids inside 2020 NTA land polygons",
            "land_cells": len(cells),
            "scored_cells": len(rows),
            "coverage_fraction": round(coverage, 6),
        },
        "raw_value_summary": {
            "min": round(float(raw.min()), 6),
            "median": round(float(np.median(raw)), 6),
            "max": round(float(raw.max()), 6),
        },
    }
    return rows, build


def publish(rows: list[tuple], build: dict, ready_root: Path) -> dict:
    out = ready_root / OUTPUT_RELPATH
    manifest_path = ready_root / MANIFEST_RELPATH
    out.parent.mkdir(parents=True, exist_ok=True)
    temp_out = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    temp_manifest = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE artifact (h3_r9 VARCHAR, raw_count DOUBLE, score INTEGER)")
        con.executemany("INSERT INTO artifact VALUES (?, ?, ?)", rows)
        con.execute(
            f"COPY artifact TO '{temp_out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        columns = [
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{temp_out.as_posix()}')"
            ).fetchall()
        ]
        row_count, unique_cells, min_score, max_score = con.execute(
            f"SELECT count(*), count(DISTINCT h3_r9), min(score), max(score) "
            f"FROM read_parquet('{temp_out.as_posix()}')"
        ).fetchone()
        if columns != ARTIFACT_COLUMNS or row_count != len(rows) or unique_cells != row_count:
            raise ValueError("NYCCAS output schema, row count or H3 uniqueness check failed")
        if min_score < 0 or max_score > 100:
            raise ValueError("NYCCAS score range check failed")
        manifest = {
            "schema_version": "1.0",
            "methodology_version": METHODOLOGY_VERSION,
            "generated": date.today().isoformat(),
            "source_dataset_id": "q68s-8qxv",
            "artifact": {
                "path": OUTPUT_RELPATH.name,
                "sha256": sha256(temp_out),
                "size_bytes": temp_out.stat().st_size,
                "row_count": row_count,
                "columns": columns,
            },
            **build,
            "limitations": [
                "Modeled annual-average surface, not short-term or regulatory monitoring.",
                "H3 values are native-raster pixel lookups at cell centroids; no interpolation.",
                "One land H3 centroid falls on source nodata and remains missing.",
            ],
        }
        temp_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temp_out.replace(out)
        temp_manifest.replace(manifest_path)
        return manifest
    finally:
        con.close()
        temp_out.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=Path("/mnt/data/urban-dossier-state/datasets/raw-expansion/nyccas"),
    )
    parser.add_argument(
        "--boundary",
        type=Path,
        default=repo_root / "data" / "boundaries" / "nta_2020.geojson",
    )
    parser.add_argument("--ready-root", type=Path, default=repo_root / "data" / "ready")
    args = parser.parse_args()
    rows, build = build_rows(args.raw_dir, args.boundary)
    manifest = publish(rows, build, args.ready_root)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
