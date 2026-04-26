"""Build NTA-level overview tiles from existing H3 r8 overview data.

Reads the H3 r8 overview parquets (built by build_overview_tiles.py) and
aggregates them into NTA (Neighborhood Tabulation Area) zones by assigning
each H3 r8 centroid to its enclosing NTA polygon.

Inputs  : data/cache/overview/overview_{tag}_h3_r8.parquet
          data/boundaries/nta_2020.geojson
Outputs : data/cache/overview/overview_{tag}_nta.parquet

Each output has the column schema:

    nta_code        str   (e.g. "BK0101")
    nta_name        str   (e.g. "Greenpoint")
    borough         str   (e.g. "Brooklyn")
    nta_type        str   (0=residential, 9=park, etc.)
    latitude        float (centroid of NTA polygon)
    longitude       float (centroid of NTA polygon)
    overall_score   float 0..100
    safety_score    float
    transit_score   float
    amenities_score float
    cell_count      int   (number of H3 r8 cells aggregated)
    risk_level      str   low|moderate|high

Backend selection
-----------------
v2 of this script uses cuDF when RAPIDS is available on the host (DGX Spark
GB10 ARM64), and transparently falls back to pandas otherwise (e.g. on a dev
Mac where cuDF is not installed). Both backends expose the same
``read_parquet`` / ``groupby().agg()`` / ``to_parquet`` surface, so the
per-tag pipeline is written once against the ``xdf`` alias. The
geometry-heavy point-in-polygon assignment stays on pandas because shapely
is CPU-only.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from shapely.geometry import MultiPolygon, Point, Polygon, shape

# --------------------------------------------------------------------------- #
# Backend selection: cuDF (GPU) preferred, pandas (CPU) fallback.
#
# The try/except keeps the module loadable on a Mac (where cuDF is not
# installed and the pandas alias kicks in). Any function that needs cuDF
# specifically (e.g. ``cudf.from_pandas``) re-imports it lazily so the
# top-level import stays optional.
# --------------------------------------------------------------------------- #
try:
    import cudf as xdf  # type: ignore[import-not-found]

    GPU = True
except ImportError:
    import pandas as xdf  # type: ignore[no-redef]

    GPU = False

logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_NTA_PATH = REPO_ROOT / "data" / "boundaries" / "nta_2020.geojson"
DEFAULT_OVERVIEW_ROOT = REPO_ROOT / "data" / "cache" / "overview"

TAGS = ("overall", "safety", "transit", "amenities")
SCORE_COLS = ("overall_score", "safety_score", "transit_score", "amenities_score")


def _load_nta_polygons(nta_path: Path) -> list[dict[str, Any]]:
    """Load NTA GeoJSON and return list of {code, name, borough, nta_type, geometry, centroid}.

    Geometry parsing is shapely-based (CPU-only), so we use plain JSON +
    shapely here even when the rest of the pipeline runs on cuDF.
    """
    with open(nta_path) as f:
        data = json.load(f)

    ntas: list[dict[str, Any]] = []
    for feat in data["features"]:
        props = feat["properties"]
        geom = shape(feat["geometry"])
        centroid = geom.centroid
        ntas.append({
            "nta_code": props.get("nta2020", ""),
            "nta_name": props.get("ntaname", ""),
            "borough": props.get("boroname", ""),
            "nta_type": props.get("ntatype", "0"),
            "geometry": geom,
            "latitude": round(centroid.y, 6),
            "longitude": round(centroid.x, 6),
        })
    return ntas


def _to_pandas(df: Any) -> pd.DataFrame:
    """Cast a cuDF DataFrame down to pandas for shapely-driven steps."""
    if hasattr(df, "to_pandas"):
        return df.to_pandas()
    return df


def _assign_h3_to_nta(h3_df: Any, ntas: list[dict[str, Any]]) -> Any:
    """Assign each H3 r8 cell centroid to the NTA polygon that contains it.

    Uses a simple point-in-polygon test. For cells that fall outside all NTAs
    (e.g. over water), the cell is assigned to the nearest NTA centroid.

    The geometry test is shapely-based (CPU-only). When the input is a cuDF
    DataFrame we materialize to pandas for this stage and re-upload the
    result so the downstream groupby can stay on the GPU when available.
    """
    from shapely.strtree import STRtree

    started_on_gpu = hasattr(h3_df, "to_pandas")
    h3_pdf = _to_pandas(h3_df)

    geoms = [nta["geometry"] for nta in ntas]
    tree = STRtree(geoms)

    assignments: list[str] = []
    for _, row in h3_pdf.iterrows():
        pt = Point(row["longitude"], row["latitude"])
        idx = tree.query(pt, predicate="contains")
        if len(idx) > 0:
            assignments.append(ntas[idx[0]]["nta_code"])
        else:
            # Fallback: nearest NTA
            nearest_idx = tree.nearest(pt)
            assignments.append(ntas[nearest_idx]["nta_code"])

    h3_pdf = h3_pdf.copy()
    h3_pdf["nta_code"] = assignments

    if started_on_gpu:
        # Re-upload to cuDF so the next groupby runs on the GPU.
        try:
            import cudf  # type: ignore[import-not-found]

            return cudf.from_pandas(h3_pdf)
        except ImportError:
            return h3_pdf
    return h3_pdf


def _aggregate_to_nta(h3_df: Any, ntas: list[dict[str, Any]]) -> pd.DataFrame:
    """Aggregate H3 r8 scores to NTA level.

    Returns a pandas DataFrame regardless of input backend: the final stage
    needs Python-level dict mapping (NTA metadata) and the ``risk_level``
    derivation, both of which are simpler in pandas. The expensive groupby
    itself runs on whichever backend ``h3_df`` arrived on.
    """
    nta_lookup = {nta["nta_code"]: nta for nta in ntas}

    grouped = h3_df.groupby("nta_code").agg(
        overall_score=("overall_score", "mean"),
        safety_score=("safety_score", "mean"),
        transit_score=("transit_score", "mean"),
        amenities_score=("amenities_score", "mean"),
        cell_count=("h3", "count"),
    ).reset_index()

    # Drop back to pandas for metadata join + risk-level mapping. cuDF's
    # ``Series.map(callable)`` is more limited than pandas's, so we hop off
    # the GPU here.
    grouped_pdf: pd.DataFrame = _to_pandas(grouped)

    # Round scores
    for col in SCORE_COLS:
        grouped_pdf[col] = grouped_pdf[col].round(0).astype("Int64")

    # Add NTA metadata
    grouped_pdf["nta_name"] = grouped_pdf["nta_code"].map(lambda c: nta_lookup.get(c, {}).get("nta_name", ""))
    grouped_pdf["borough"] = grouped_pdf["nta_code"].map(lambda c: nta_lookup.get(c, {}).get("borough", ""))
    grouped_pdf["nta_type"] = grouped_pdf["nta_code"].map(lambda c: nta_lookup.get(c, {}).get("nta_type", "0"))
    grouped_pdf["latitude"] = grouped_pdf["nta_code"].map(lambda c: nta_lookup.get(c, {}).get("latitude", 0.0))
    grouped_pdf["longitude"] = grouped_pdf["nta_code"].map(lambda c: nta_lookup.get(c, {}).get("longitude", 0.0))

    # Risk level
    grouped_pdf["risk_level"] = grouped_pdf["overall_score"].map(
        lambda s: "unknown" if pd.isna(s) else "low" if s >= 65 else "moderate" if s >= 40 else "high"
    )

    return grouped_pdf


def build_nta_tiles(nta_path: Path, overview_root: Path) -> dict[str, int]:
    """Build NTA-level overview tiles from H3 r8 overview data."""
    logger.info(
        "build_overview_nta using %s backend",
        "cuDF (GPU)" if GPU else "pandas (CPU)",
    )
    print(f"[build_overview_nta] backend: {'cuDF (GPU)' if GPU else 'pandas (CPU)'}")

    ntas = _load_nta_polygons(nta_path)
    print(f"[build_overview_nta] loaded {len(ntas)} NTA polygons")

    counts: dict[str, int] = {}

    for tag in TAGS:
        h3_path = overview_root / f"overview_{tag}_h3_r8.parquet"
        if not h3_path.exists():
            print(f"[build_overview_nta] skipping {tag}: {h3_path.name} not found")
            continue

        # ``xdf.read_parquet`` resolves to ``cudf.read_parquet`` on DGX Spark,
        # ``pd.read_parquet`` everywhere else.
        h3_df: Any = xdf.read_parquet(h3_path)
        print(f"[build_overview_nta] {tag}: {len(h3_df)} H3 r8 cells")

        # Assign H3 cells to NTAs (shapely-based, hops off GPU mid-pipeline)
        h3_with_nta = _assign_h3_to_nta(h3_df, ntas)

        # Aggregate (groupby on whichever backend the dataframe lives on)
        nta_df = _aggregate_to_nta(h3_with_nta, ntas)

        # Write output - canonical column order is fixed by downstream contract.
        out_cols = [
            "nta_code", "nta_name", "borough", "nta_type",
            "latitude", "longitude",
            "overall_score", "safety_score", "transit_score", "amenities_score",
            "cell_count", "risk_level",
        ]
        out_path = overview_root / f"overview_{tag}_nta.parquet"
        # ``nta_df`` is always pandas after _aggregate_to_nta; pandas's own
        # ``to_parquet`` is the safe write path for both backends.
        nta_df[out_cols].to_parquet(out_path, index=False)

        # Also write JSON for direct consumption by server.js (no backend API needed)
        json_path = overview_root / f"overview_{tag}_nta.json"
        records = nta_df[out_cols].to_dict(orient="records")
        # Convert Int64 NA to None for JSON compatibility
        for rec in records:
            for k, v in rec.items():
                if pd.isna(v):
                    rec[k] = None
                elif hasattr(v, "item"):
                    rec[k] = v.item()
        import json as _json
        json_path.write_text(_json.dumps(records, separators=(",", ":")))

        counts[tag] = len(nta_df)
        print(f"[build_overview_nta] wrote {out_path.name} + {json_path.name} with {len(nta_df)} NTAs (from {len(h3_df)} H3 cells)")

    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nta-path", type=Path, default=DEFAULT_NTA_PATH)
    parser.add_argument("--overview-root", type=Path, default=DEFAULT_OVERVIEW_ROOT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    build_nta_tiles(args.nta_path, args.overview_root)


if __name__ == "__main__":
    main()
