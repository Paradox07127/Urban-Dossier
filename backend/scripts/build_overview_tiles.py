"""Build the H3 r8 overview tile set consumed by /api/overview.

The runtime (`DirectQueryDataProvider.get_overview_layer`) reads parquet files
from ``$CACHE_DIR/overview/overview_{tag}_h3_r8.parquet``. This script
aggregates the v3.7.8 ready score tables from H3 r9 up to H3 r8 (each parent
cell contains ~7 children), produces per-category aggregated scores with a
centroid ``latitude``/``longitude`` for easy map rendering, and writes one
tile file per tag (overall / safety / transit / amenities).

Inputs  : data/ready/{safety,transit,amenities,building}/*_scores_h3.parquet
Outputs : data/cache/overview/overview_{overall,safety,transit,amenities}_h3_r8.parquet

Each output has the column schema the Node proxy and the React frontend look
for:

    h3              str   (r8 cell id)
    latitude        float (centroid)
    longitude       float (centroid)
    cell_id         str   (alias for h3, used by older clients)
    overall_score   int   0..100
    safety_score    int
    transit_score   int
    amenities_score int
    risk_level      str   low|moderate|high
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import cudf.pandas
    cudf.pandas.install()
except ImportError:
    pass

import pandas as pd
from h3 import cell_to_latlng, cell_to_parent


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_READY_ROOT = REPO_ROOT / "data" / "ready"
DEFAULT_OVERVIEW_ROOT = REPO_ROOT / "data" / "cache" / "overview"
R8 = 8

# Rough NYC bounding box (five boroughs + a small margin). Any r8 centroid
# outside this box is dropped to stop the handful of typoed CSV rows with
# lat/lon like (34.78, -86.76) from producing phantom overview cells in
# Alabama or the middle of the Atlantic.
NYC_BBOX = {
    "lat_min": 40.45,
    "lat_max": 40.95,
    "lon_min": -74.30,
    "lon_max": -73.65,
}


# (tag -> list of (weight, parquet relative path)). Only H3-scored datasets are
# included. ZIP-scored tables (ems/fire/parks) are not aggregated into r8
# tiles; they still contribute to point-level preview.
#
# Derived from the metric registry, never written by hand. A hand-maintained
# copy of this mapping lived here until 2026-08-12 and was the last of three
# independent weight tables in the repo: it carried its own numbers that never
# matched CATEGORY_CONFIG in any era, and it kept reading the collision copy
# and the count-based rodent table long after the registry retired both -- so
# the low-zoom overview served one methodology while the buildings and the
# detail panel served another, with nothing to say so. Deriving here is the
# same cure categories.py got in v3.8.0: a weight has exactly one home.
#
# ZIP-grain metrics are excluded and the remaining H3 weights renormalise
# within each category -- the documented decision this script always embodied,
# now computed instead of transcribed.


def _registry_sources() -> tuple[dict[str, list[tuple[float, str]]], dict[str, float]]:
    from urban_dossier_backend.metrics import CATEGORIES, metrics_for_category

    sources: dict[str, list[tuple[float, str]]] = {}
    overall: dict[str, float] = {}
    for category in CATEGORIES:
        if not category.map_driving or category.weight_in_overall <= 0:
            continue
        h3_metrics = [
            m for m in metrics_for_category(category.id)
            if m.spatial_grain.value == "h3_r9"
        ]
        weight_sum = sum(m.weight_in_category for m in h3_metrics)
        if weight_sum <= 0:
            continue
        sources[category.id] = [
            (m.weight_in_category / weight_sum, m.score_table) for m in h3_metrics
        ]
        overall[category.id] = category.weight_in_overall
    total = sum(overall.values())
    return sources, {k: v / total for k, v in overall.items()}


CATEGORY_SOURCES, OVERALL_WEIGHTS = _registry_sources()


def _aggregate_to_r8(path: Path) -> pd.DataFrame | None:
    """Read an r9 score table and average the ``score`` column to r8 cells."""
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["h3_r9", "score"])
    if df.empty or "h3_r9" not in df.columns:
        return None
    df = df.dropna(subset=["h3_r9"]).copy()
    df["h3_r8"] = df["h3_r9"].map(lambda cell: cell_to_parent(cell, R8))
    return (
        df.groupby("h3_r8")["score"]
        .mean()
        .reset_index()
        .rename(columns={"score": "score"})
    )


def _category_tile(category: str, ready_root: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    weights: list[float] = []
    for weight, rel in CATEGORY_SOURCES[category]:
        aggregated = _aggregate_to_r8(ready_root / rel)
        if aggregated is None or aggregated.empty:
            continue
        aggregated = aggregated.rename(columns={"score": f"score_{len(frames)}"})
        frames.append(aggregated)
        weights.append(weight)
    if not frames:
        return pd.DataFrame(columns=["h3_r8", f"{category}_score"])

    merged = frames[0]
    for other in frames[1:]:
        merged = merged.merge(other, on="h3_r8", how="outer")

    # Weighted average across only the sub-datasets that had data for a cell.
    score_cols = [col for col in merged.columns if col.startswith("score_")]
    values = merged[score_cols].to_numpy()
    weight_row = pd.Series(weights).to_numpy()
    mask = ~pd.isna(values)
    masked_values = values.copy()
    masked_values[~mask] = 0
    masked_weights = mask.astype(float) * weight_row
    weight_sum = masked_weights.sum(axis=1)
    weighted_sum = (masked_values * masked_weights).sum(axis=1)
    merged[f"{category}_score"] = pd.Series(
        [
            round(wsum / wsum_total) if wsum_total > 0 else None
            for wsum, wsum_total in zip(weighted_sum, weight_sum)
        ]
    )
    return merged[["h3_r8", f"{category}_score"]].dropna(subset=[f"{category}_score"])


def _add_centroid(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.assign(latitude=[], longitude=[])
    lat_lon = [cell_to_latlng(cell) for cell in df["h3_r8"]]
    df = df.copy()
    df["latitude"] = [round(lat, 6) for lat, _ in lat_lon]
    df["longitude"] = [round(lon, 6) for _, lon in lat_lon]
    df["h3"] = df["h3_r8"]
    df["cell_id"] = df["h3_r8"]
    return df


def _risk_level(score: float | None) -> str:
    if score is None or pd.isna(score):
        return "unknown"
    if score >= 65:
        return "low"
    if score >= 40:
        return "moderate"
    return "high"


def _merge_all_categories(ready_root: Path) -> pd.DataFrame:
    category_tiles = {cat: _category_tile(cat, ready_root) for cat in CATEGORY_SOURCES}
    merged: pd.DataFrame | None = None
    for cat, tile in category_tiles.items():
        if tile.empty:
            continue
        if merged is None:
            merged = tile
        else:
            merged = merged.merge(tile, on="h3_r8", how="outer")
    if merged is None:
        return pd.DataFrame(columns=["h3_r8"])

    # Fill missing per-category score columns so downstream math is consistent.
    for cat in CATEGORY_SOURCES:
        col = f"{cat}_score"
        if col not in merged.columns:
            merged[col] = None

    # Weighted overall: skip categories with no data for that cell.
    overall: list[float | None] = []
    for _, row in merged.iterrows():
        num, den = 0.0, 0.0
        for cat, weight in OVERALL_WEIGHTS.items():
            val = row.get(f"{cat}_score")
            if val is None or pd.isna(val):
                continue
            num += float(val) * weight
            den += weight
        overall.append(round(num / den) if den > 0 else None)
    merged["overall_score"] = overall
    return merged


def build_tiles(ready_root: Path, overview_root: Path) -> dict[str, int]:
    overview_root.mkdir(parents=True, exist_ok=True)
    merged = _merge_all_categories(ready_root)
    merged = _add_centroid(merged)

    # Drop cells with literally no usable data (no overall + no category scores).
    score_cols = ["overall_score", "safety_score", "transit_score", "amenities_score"]
    if merged.empty or not set(score_cols).issubset(merged.columns):
        print("[build_overview_tiles] no score columns available - skipping")
        return {}
    merged = merged.dropna(subset=score_cols, how="all").reset_index(drop=True)

    # Drop centroids that fell outside NYC. These are always upstream typos
    # (bad CSV rows with lat=0 or lat=34.78 etc) that survive the H3 encoding
    # because latlng_to_cell is happy with any globe coordinate.
    before = len(merged)
    merged = merged[
        merged["latitude"].between(NYC_BBOX["lat_min"], NYC_BBOX["lat_max"]) &
        merged["longitude"].between(NYC_BBOX["lon_min"], NYC_BBOX["lon_max"])
    ].reset_index(drop=True)
    dropped = before - len(merged)
    if dropped:
        print(f"[build_overview_tiles] dropped {dropped} non-NYC cells (bad upstream coordinates)")

    merged["risk_level"] = merged["overall_score"].map(_risk_level)

    counts: dict[str, int] = {}

    def _write(tag: str, df: pd.DataFrame) -> None:
        path = overview_root / f"overview_{tag}_h3_r8.parquet"
        df.to_parquet(path, index=False)
        counts[tag] = len(df)
        print(f"[build_overview_tiles] wrote {path.name} with {len(df)} cells")

    # Overall tile: every cell with any overall_score.
    overall_df = merged.dropna(subset=["overall_score"]).copy()
    _write("overall", overall_df[["h3", "cell_id", "latitude", "longitude", "overall_score", "safety_score", "transit_score", "amenities_score", "risk_level"]])

    for tag in ("safety", "transit", "amenities"):
        col = f"{tag}_score"
        tag_df = merged.dropna(subset=[col]).copy()
        _write(tag, tag_df[["h3", "cell_id", "latitude", "longitude", "overall_score", "safety_score", "transit_score", "amenities_score", "risk_level"]])

    return counts


def write_manifest(overview_root: Path, counts: dict[str, int]) -> None:
    """Version stamp beside the artifacts, so staleness is detectable.

    The August artifacts had no version field, which is why a methodology
    change could leave them serving silently. The runtime and the
    reconciliation test both compare this against the code's version.
    """
    from urban_dossier_backend.metrics import METHODOLOGY_VERSION

    manifest = {
        "methodology_version": METHODOLOGY_VERSION,
        "generated": date.today().isoformat(),
        "category_sources": {
            tag: [[round(w, 4), rel] for w, rel in pairs]
            for tag, pairs in CATEGORY_SOURCES.items()
        },
        "overall_weights": {k: round(v, 4) for k, v in OVERALL_WEIGHTS.items()},
        "cells": counts,
    }
    (overview_root / "overview.manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-root", type=Path, default=DEFAULT_READY_ROOT)
    parser.add_argument("--overview-root", type=Path, default=DEFAULT_OVERVIEW_ROOT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    counts = build_tiles(args.ready_root, args.overview_root)
    write_manifest(args.overview_root, counts)
    print(json.dumps({"methodology_version_stamped": True, **counts}))


if __name__ == "__main__":
    main()
