"""GPU-accelerated query functions using cuDF (RAPIDS).

Drop-in replacements for DuckDB queries. Falls back gracefully when cuDF
is unavailable.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CUDF_AVAILABLE = False
_cudf = None

try:
    import cudf as _cudf  # type: ignore[import-untyped]
    _CUDF_AVAILABLE = True
    logger.info("cuDF available — GPU query path enabled")
except ImportError:
    pass

_NYC_COS_LAT = 0.7580107
_DEG_TO_M = 111_320.0


def is_available() -> bool:
    return _CUDF_AVAILABLE


_FALLBACK_SENTINEL: list[dict[str, Any]] = [{"__gpu_fallback__": True}]


def is_fallback(result: list[dict[str, Any]]) -> bool:
    return result is _FALLBACK_SENTINEL


def gpu_fetch_radius_rows(
    parquet_path: Path,
    lat_col: str,
    lon_col: str,
    lat: float,
    lon: float,
    radius_m: float,
    columns: list[str],
    limit: int = 50000,
) -> list[dict[str, Any]]:
    """Read parquet on GPU, bbox + flat-earth filter, return dicts."""
    if not _CUDF_AVAILABLE or not parquet_path.exists():
        return _FALLBACK_SENTINEL

    all_columns = list(set([lat_col, lon_col] + columns))
    try:
        df = _cudf.read_parquet(str(parquet_path), columns=all_columns)
    except Exception:
        return _FALLBACK_SENTINEL

    try:
        lat_s = _cudf.to_numeric(df[lat_col], errors="coerce")
        lon_s = _cudf.to_numeric(df[lon_col], errors="coerce")
    except KeyError:
        return _FALLBACK_SENTINEL

    valid = lat_s.notna() & lon_s.notna()
    df = df[valid]
    lat_s = lat_s[valid]
    lon_s = lon_s[valid]

    # Bounding box pre-filter
    lat_delta = radius_m / _DEG_TO_M
    lon_delta = radius_m / (_DEG_TO_M * _NYC_COS_LAT)
    bbox_mask = (
        (lat_s >= lat - lat_delta) & (lat_s <= lat + lat_delta)
        & (lon_s >= lon - lon_delta) & (lon_s <= lon + lon_delta)
    )
    df = df[bbox_mask]
    lat_s = lat_s[bbox_mask]
    lon_s = lon_s[bbox_mask]

    if len(df) == 0:
        return []

    # Flat-earth squared distance filter
    dlat = (lat_s - lat) * _DEG_TO_M
    dlon = (lon_s - lon) * _DEG_TO_M * _NYC_COS_LAT
    dist_sq = dlat * dlat + dlon * dlon
    within = dist_sq <= radius_m * radius_m

    df = df[within]
    lat_s = lat_s[within]
    lon_s = lon_s[within]

    if len(df) == 0:
        return []

    df = df.head(limit)
    result_df = df.copy()
    result_df["__lat"] = lat_s.head(limit)
    result_df["__lon"] = lon_s.head(limit)

    return result_df.to_pandas().to_dict("records")


def gpu_nearest_overview_cell(
    rows: list[dict[str, Any]],
    latitude: float,
    longitude: float,
    lat_keys: tuple[str, ...] = ("latitude", "lat", "center_lat", "centroid_lat"),
    lon_keys: tuple[str, ...] = ("longitude", "lng", "center_lng", "centroid_lng"),
) -> dict[str, Any] | None:
    """Find the nearest row to (latitude, longitude) using GPU.

    Extracts only lat/lon columns into cudf for distance calculation,
    then returns the original dict from the rows list to avoid mixed-type issues.
    """
    if not _CUDF_AVAILABLE or not rows:
        return None

    # Find lat/lon column names from the first row
    first = rows[0]
    lat_col = next((k for k in lat_keys if k in first), None)
    lon_col = next((k for k in lon_keys if k in first), None)
    if lat_col is None or lon_col is None:
        return None

    # Extract only numeric lat/lon into cudf Series (avoids mixed-type DataFrame)
    try:
        lat_vals = _cudf.Series([r.get(lat_col) for r in rows], dtype="float64", nan_as_null=True)
        lon_vals = _cudf.Series([r.get(lon_col) for r in rows], dtype="float64", nan_as_null=True)
    except Exception:
        return None

    valid = lat_vals.notna() & lon_vals.notna()
    if not valid.any():
        return None

    # Build index mapping: position in valid array → position in original rows
    valid_indices = _cudf.Series(range(len(rows)))[valid].reset_index(drop=True)
    lat_v = lat_vals[valid].reset_index(drop=True)
    lon_v = lon_vals[valid].reset_index(drop=True)

    dlat = (lat_v - latitude) * _DEG_TO_M
    dlon = (lon_v - longitude) * _DEG_TO_M * _NYC_COS_LAT
    dist_sq = dlat * dlat + dlon * dlon

    min_pos = int(dist_sq.values.argmin())
    original_idx = int(valid_indices.iloc[min_pos])
    return rows[original_idx]


def gpu_emergency_metrics(
    parquet_path: Path,
    zip_code: str,
    response_col: str = "INCIDENT_RESPONSE_SECONDS_QY",
    zip_col: str = "ZIPCODE",
) -> tuple[float | None, int]:
    """GPU-accelerated emergency dispatch aggregation for a ZIP code.

    Returns (avg_response_seconds, count) or (None, 0) on failure/no data.
    """
    if not _CUDF_AVAILABLE or not parquet_path.exists():
        return None, 0

    try:
        # Read only needed columns
        df = _cudf.read_parquet(str(parquet_path), columns=[zip_col, response_col])

        # Cast and filter
        df[response_col] = _cudf.to_numeric(df[response_col], errors="coerce")
        df[zip_col] = df[zip_col].astype(str)

        filtered = df[df[zip_col] == str(zip_code)]
        filtered = filtered[filtered[response_col] > 0]
        filtered = filtered.dropna(subset=[response_col])

        if len(filtered) == 0:
            return None, 0

        avg_resp = float(filtered[response_col].mean())
        count = int(len(filtered))
        return avg_resp, count
    except Exception as e:
        logger.warning("gpu_emergency_metrics failed for %s: %s", parquet_path, e)
        return None, 0
