#!/usr/bin/env python3
"""
Data cleaning executor for nemoclaw-user-prep-data (pandas, CSV-only).

Accepts a CSV file and a list of explicit, parameterized operations.
Does NOT make any decisions — only executes what it is told.
All output goes to a new file; originals are never modified.

Security model
--------------
This script is invoked by an LLM-driven agent. The agent is the sole input
source for `--ops`, so every value reaching pandas must be sanitized at the
script boundary. Pandas does not have prepared statements, so the defense
shifts from "bind variables" to "never call pandas eval/query with LLM
input + always validate column names against df.columns + enum-whitelist
every operation parameter".

  1. Column names — NEVER passed through unchecked. Validated against the
     live `df.columns` whitelist via `_checked_col`.
  2. Cast target types and case modes — enum whitelists.
  3. Filter conditions — enum-mapped to fixed pandas operations.
  4. NEVER call `df.eval()` or `df.query()` with LLM-generated strings.
     We don't expose any op that takes a free-form expression.

Operation vocabulary is identical to the DuckDB version (12 ops, same
param shapes), so any existing prompts or demo cache references still
work without modification.

Usage:
    python clean.py <input.csv> <output.csv> --ops '<json_array>' [--demo]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


DEMO_CACHE_DIR = Path(__file__).parent / "demo_cache"

# Whitelisted target types for cast_type. Anything else is rejected.
# We map these to pandas/numpy dtypes inside _op_cast_type.
ALLOWED_CAST_TYPES = {
    "BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "REAL", "DOUBLE", "DECIMAL",
    "VARCHAR", "TEXT", "BLOB",
    "DATE", "TIME", "TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS",
    "INTERVAL", "UUID", "JSON",
}

ALLOWED_CASE_MODES = {"lower", "upper", "title"}

ALLOWED_FILTER_CONDITIONS = {
    "gt", "lt", "gte", "lte", "eq", "neq",
    "in", "not_in", "between", "is_null", "not_null",
}

CAST_TYPE_TO_PANDAS = {
    "BOOLEAN": "boolean",
    "TINYINT": "Int8", "SMALLINT": "Int16", "INTEGER": "Int32", "BIGINT": "Int64",
    "HUGEINT": "Int64",
    "UTINYINT": "UInt8", "USMALLINT": "UInt16", "UINTEGER": "UInt32", "UBIGINT": "UInt64",
    "FLOAT": "float32", "REAL": "float32", "DOUBLE": "float64",
    "DECIMAL": "float64",  # pandas has no fixed-point; degrade to float64
    "VARCHAR": "string", "TEXT": "string", "BLOB": "string",
    "DATE": "datetime64[ns]",
    "TIME": "string",      # pandas has no native time-of-day; keep as string
    "TIMESTAMP": "datetime64[ns]",
    "TIMESTAMP_S": "datetime64[s]",
    "TIMESTAMP_MS": "datetime64[ms]",
    "TIMESTAMP_NS": "datetime64[ns]",
    "INTERVAL": "timedelta64[ns]",
    "UUID": "string",
    "JSON": "string",
}


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def _detect_format(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext != ".csv":
        raise ValueError(
            f"Unsupported file format: {ext}. This skill only supports CSV. "
            f"Convert your file to CSV first."
        )
    return "csv"


def _load_source(path: str) -> pd.DataFrame:
    """Load the source CSV into a pandas DataFrame."""
    _detect_format(path)
    last_err: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(
                path,
                encoding=encoding,
                low_memory=False,
                keep_default_na=True,
                na_values=["", "NA", "N/A", "n/a", "null", "NULL", "None"],
            )
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise UnicodeDecodeError("utf-8", b"", 0, 0, str(last_err) if last_err else "encoding")


# ---------------------------------------------------------------------------
# Identifier / parameter validators — the entire injection defense lives here
# ---------------------------------------------------------------------------

def _checked_col(df: pd.DataFrame, name: object) -> str:
    """Validate that `name` is a real column in df. Returns the column name."""
    if not isinstance(name, str):
        raise ValueError(f"Column name must be a string, got {type(name).__name__}")
    if name not in df.columns:
        raise ValueError(
            f"Unknown column: {name!r}. Known columns: {sorted(df.columns)}"
        )
    return name


def _checked_type(target_type: object) -> str:
    """Validate a cast target type against the whitelist."""
    if not isinstance(target_type, str):
        raise ValueError(f"target_type must be a string, got {type(target_type).__name__}")
    normalized = target_type.strip().upper()
    head = normalized.split("(", 1)[0].strip()
    if head not in ALLOWED_CAST_TYPES:
        raise ValueError(
            f"Disallowed cast type: {target_type!r}. Allowed: {sorted(ALLOWED_CAST_TYPES)}"
        )
    if "(" in normalized:
        inside = normalized[normalized.index("(") + 1: normalized.rindex(")")]
        if not all(c.isdigit() or c in ", " for c in inside):
            raise ValueError(f"Disallowed cast type parameters: {target_type!r}")
    return head  # we don't pass parameters through to pandas


def _checked_case_mode(mode: object) -> str:
    if mode not in ALLOWED_CASE_MODES:
        raise ValueError(
            f"Disallowed case mode: {mode!r}. Allowed: {sorted(ALLOWED_CASE_MODES)}"
        )
    return mode  # type: ignore[return-value]


def _checked_condition(cond: object) -> str:
    if cond not in ALLOWED_FILTER_CONDITIONS:
        raise ValueError(
            f"Disallowed filter condition: {cond!r}. Allowed: {sorted(ALLOWED_FILTER_CONDITIONS)}"
        )
    return cond  # type: ignore[return-value]


def _checked_new_col(df: pd.DataFrame, name: object) -> str:
    """Validate that `name` is a NEW column — non-empty string and not present."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"Output column name must be a non-empty string, got {name!r}")
    if name in df.columns:
        raise ValueError(f"Output column {name!r} already exists in data")
    return name


def _checked_epsg(crs: object) -> str:
    if not isinstance(crs, str):
        raise ValueError(f"CRS must be a string, got {type(crs).__name__}")
    if not re.fullmatch(r"EPSG:\d+", crs):
        raise ValueError(f"CRS must match 'EPSG:<digits>', got {crs!r}")
    return crs


def _checked_int_range(val: object, name: str, lo: int, hi: int) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{name} must be an int, got {type(val).__name__}")
    if val < lo or val > hi:
        raise ValueError(f"{name} must be in [{lo}, {hi}], got {val}")
    return val


# ---------------------------------------------------------------------------
# Operation executors
# ---------------------------------------------------------------------------

def _op_drop_column(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    df = df.drop(columns=[col])
    return df, f"Dropped column {col!r}"


def _op_rename_column(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    old = _checked_col(df, params.get("old_name"))
    new = params.get("new_name")
    if not isinstance(new, str) or not new:
        raise ValueError("rename_column requires a non-empty 'new_name' string")
    if new in df.columns and new != old:
        raise ValueError(f"rename target {new!r} already exists")
    df = df.rename(columns={old: new})
    return df, f"Renamed column {old!r} -> {new!r}"


def _op_replace_values(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    mapping = params.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("replace_values requires a non-empty 'mapping' dict")
    # pandas Series.replace handles non-string keys natively; mapping values are
    # passed through verbatim. Because we're not using eval/query/format strings
    # there's no injection surface.
    df = df.copy()
    df[col] = df[col].replace(mapping)
    return df, f"Replaced {len(mapping)} values in column {col!r}"


def _op_cast_type(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    target = _checked_type(params.get("target_type"))
    pd_dtype = CAST_TYPE_TO_PANDAS.get(target)
    if pd_dtype is None:
        raise ValueError(f"No pandas mapping for cast target {target!r}")

    df = df.copy()
    series = df[col]
    try:
        if pd_dtype.startswith("datetime") or target in ("DATE", "TIMESTAMP",
                                                          "TIMESTAMP_S",
                                                          "TIMESTAMP_MS",
                                                          "TIMESTAMP_NS"):
            df[col] = pd.to_datetime(series, errors="coerce", format="mixed")
        elif pd_dtype.startswith("timedelta"):
            df[col] = pd.to_timedelta(series, errors="coerce")
        elif pd_dtype in ("Int8", "Int16", "Int32", "Int64",
                          "UInt8", "UInt16", "UInt32", "UInt64"):
            coerced = pd.to_numeric(series, errors="coerce")
            df[col] = coerced.astype(pd_dtype, errors="ignore")
        elif pd_dtype in ("float32", "float64"):
            df[col] = pd.to_numeric(series, errors="coerce").astype(pd_dtype)
        elif pd_dtype == "boolean":
            # Lenient boolean coercion
            mapping = {"true": True, "false": False, "yes": True, "no": False,
                       "y": True, "n": False, "1": True, "0": False, "t": True, "f": False}
            df[col] = (
                series.astype("string")
                .str.lower()
                .map(mapping)
                .astype("boolean")
            )
        elif pd_dtype == "string":
            df[col] = series.astype("string")
        else:
            df[col] = series.astype(pd_dtype, errors="ignore")
    except Exception as e:
        raise ValueError(f"cast_type failed: {type(e).__name__}: {e}")
    return df, f"Cast column {col!r} to {target}"


def _op_drop_duplicates(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    subset = params.get("subset")
    if subset:
        if not isinstance(subset, list) or not all(isinstance(c, str) for c in subset):
            raise ValueError("drop_duplicates 'subset' must be a list of strings")
        for c in subset:
            _checked_col(df, c)
        df = df.drop_duplicates(subset=subset, keep="first").reset_index(drop=True)
        return df, f"Dropped duplicates on columns: {subset}"
    df = df.drop_duplicates(keep="first").reset_index(drop=True)
    return df, "Dropped full-row duplicates"


def _op_drop_nulls(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    df = df.dropna(subset=[col]).reset_index(drop=True)
    return df, f"Dropped rows where {col!r} is null"


def _op_filter_rows(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    cond = _checked_condition(params.get("condition"))
    series = df[col]

    if cond == "is_null":
        mask = series.isna()
    elif cond == "not_null":
        mask = series.notna()
    elif cond in ("in", "not_in"):
        vals = params.get("values")
        if not isinstance(vals, list) or not vals:
            raise ValueError(f"filter_rows {cond!r} requires non-empty 'values' list")
        mask = series.isin(vals)
        if cond == "not_in":
            mask = ~mask
    elif cond == "between":
        v1 = params.get("value")
        v2 = params.get("value2")
        if v1 is None or v2 is None:
            raise ValueError("filter_rows 'between' requires both 'value' and 'value2'")
        mask = series.between(v1, v2)
    else:
        v = params.get("value")
        if v is None:
            raise ValueError(f"filter_rows {cond!r} requires 'value'")
        op_map = {
            "gt": (lambda s, x: s > x),
            "lt": (lambda s, x: s < x),
            "gte": (lambda s, x: s >= x),
            "lte": (lambda s, x: s <= x),
            "eq": (lambda s, x: s == x),
            "neq": (lambda s, x: s != x),
        }
        mask = op_map[cond](series, v)

    df = df.loc[mask.fillna(False)].reset_index(drop=True)
    return df, f"Filtered rows: kept where {col!r} {cond}"


def _op_strip_whitespace(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    df = df.copy()
    df[col] = df[col].astype("string").str.strip()
    return df, f"Stripped whitespace from column {col!r}"


def _op_transform_case(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    col = _checked_col(df, params.get("column"))
    mode = _checked_case_mode(params.get("mode", "lower"))
    df = df.copy()
    s = df[col].astype("string")
    if mode == "upper":
        df[col] = s.str.upper()
    elif mode == "title":
        # Match the DuckDB version: only capitalize first character
        df[col] = s.str[0].str.upper().fillna("") + s.str[1:].str.lower().fillna("")
        # Restore NaNs that just collapsed to empty strings
        df.loc[s.isna(), col] = pd.NA
    else:
        df[col] = s.str.lower()
    return df, f"Transformed column {col!r} to {mode} case"


def _op_add_h3_index(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    import h3

    lat_col = _checked_col(df, params.get("lat_col"))
    lon_col = _checked_col(df, params.get("lon_col"))
    resolution = _checked_int_range(params.get("resolution", 9), "resolution", 0, 15)
    out_name = params.get("output_col", "h3_index")
    out_col = _checked_new_col(df, out_name)

    def _to_h3(row):
        lat = row[lat_col]
        lon = row[lon_col]
        if pd.isna(lat) or pd.isna(lon):
            return None
        try:
            return h3.latlng_to_cell(float(lat), float(lon), resolution)
        except Exception:
            return None

    df = df.copy()
    df[out_col] = df.apply(_to_h3, axis=1).astype("string")
    return df, f"Added H3 resolution-{resolution} index as {out_name!r}"


def _op_transform_coords(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    from pyproj import Transformer

    x_col = _checked_col(df, params.get("x_col"))
    y_col = _checked_col(df, params.get("y_col"))
    src = _checked_epsg(params.get("source_crs"))
    dst = _checked_epsg(params.get("target_crs", "EPSG:4326"))
    out_lon_name = params.get("out_lon_col", "longitude")
    out_lat_name = params.get("out_lat_col", "latitude")
    out_lon = _checked_new_col(df, out_lon_name)
    out_lat = _checked_new_col(df, out_lat_name)

    transformer = Transformer.from_crs(src, dst, always_xy=True)

    df = df.copy()
    xs = pd.to_numeric(df[x_col], errors="coerce")
    ys = pd.to_numeric(df[y_col], errors="coerce")
    valid_mask = xs.notna() & ys.notna()

    lons = pd.Series([np.nan] * len(df), index=df.index, dtype="float64")
    lats = pd.Series([np.nan] * len(df), index=df.index, dtype="float64")

    if valid_mask.any():
        lon_arr, lat_arr = transformer.transform(
            xs[valid_mask].to_numpy(),
            ys[valid_mask].to_numpy(),
        )
        # Replace inf with NaN
        lon_arr = np.where(np.isfinite(lon_arr), lon_arr, np.nan)
        lat_arr = np.where(np.isfinite(lat_arr), lat_arr, np.nan)
        lons.loc[valid_mask] = lon_arr
        lats.loc[valid_mask] = lat_arr

    df[out_lon] = lons
    df[out_lat] = lats
    return df, (
        f"Transformed {src} -> {dst}: ({x_col!r}, {y_col!r}) -> "
        f"({out_lon_name!r}, {out_lat_name!r})"
    )


def _op_polygon_centroid(df: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, str]:
    from shapely import wkt as _wkt

    geom_col = _checked_col(df, params.get("geom_col"))
    out_lon_name = params.get("out_lon_col", "centroid_lon")
    out_lat_name = params.get("out_lat_col", "centroid_lat")
    out_lon = _checked_new_col(df, out_lon_name)
    out_lat = _checked_new_col(df, out_lat_name)

    def _centroid(geom_str):
        if geom_str is None or (isinstance(geom_str, float) and np.isnan(geom_str)):
            return (None, None)
        try:
            g = _wkt.loads(str(geom_str))
            if g.is_empty:
                return (None, None)
            return (g.centroid.x, g.centroid.y)
        except Exception:
            return (None, None)

    df = df.copy()
    pairs = df[geom_col].apply(_centroid)
    df[out_lon] = pairs.apply(lambda p: p[0]).astype("float64")
    df[out_lat] = pairs.apply(lambda p: p[1]).astype("float64")
    return df, (
        f"Computed centroid from {geom_col!r} into "
        f"({out_lon_name!r}, {out_lat_name!r})"
    )


OPERATIONS = {
    "drop_column": _op_drop_column,
    "rename_column": _op_rename_column,
    "replace_values": _op_replace_values,
    "cast_type": _op_cast_type,
    "drop_duplicates": _op_drop_duplicates,
    "drop_nulls": _op_drop_nulls,
    "filter_rows": _op_filter_rows,
    "strip_whitespace": _op_strip_whitespace,
    "transform_case": _op_transform_case,
    "add_h3_index": _op_add_h3_index,
    "transform_coords": _op_transform_coords,
    "polygon_centroid": _op_polygon_centroid,
}


# ---------------------------------------------------------------------------
# Main cleaning logic
# ---------------------------------------------------------------------------

def clean_file(input_path: str, output_path: str, ops: list[dict]) -> dict:
    input_path = os.path.abspath(input_path)
    output_path = os.path.abspath(output_path)

    log_entries: list[dict] = []
    start_time = datetime.now(timezone.utc).isoformat()

    try:
        df = _load_source(input_path)
        initial_rows = len(df)

        for i, op in enumerate(ops):
            op_name = op.get("op")
            if op_name not in OPERATIONS:
                log_entries.append({
                    "step": i + 1,
                    "op": op_name,
                    "status": "error",
                    "reason": f"Unknown operation: {op_name}",
                })
                continue

            rows_before = len(df)
            try:
                df, description = OPERATIONS[op_name](df, op)
                rows_after = len(df)
                log_entries.append({
                    "step": i + 1,
                    "op": op_name,
                    "params": {k: v for k, v in op.items() if k != "op"},
                    "status": "ok",
                    "description": description,
                    "rows_before": rows_before,
                    "rows_after": rows_after,
                })
            except Exception as e:
                log_entries.append({
                    "step": i + 1,
                    "op": op_name,
                    "params": {k: v for k, v in op.items() if k != "op"},
                    "status": "error",
                    "reason": f"{type(e).__name__}: {e}",
                    "rows_before": rows_before,
                })

        final_rows = len(df)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        df.to_csv(output_path, index=False)

    except Exception as e:
        return {
            "status": "error",
            "input_file": input_path,
            "reason": f"{type(e).__name__}: {e}",
            "operations": log_entries,
        }

    return {
        "status": "ok",
        "input_file": input_path,
        "output_file": output_path,
        "initial_rows": initial_rows,
        "final_rows": final_rows,
        "operations_total": len(ops),
        "operations_succeeded": sum(1 for e in log_entries if e["status"] == "ok"),
        "operations_failed": sum(1 for e in log_entries if e["status"] == "error"),
        "operations": log_entries,
        "start_time": start_time,
        "end_time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Clean a CSV file with explicit operations (pandas)")
    parser.add_argument("input_file", help="Path to the input CSV file")
    parser.add_argument("output_file", help="Path for the cleaned output (CSV)")
    parser.add_argument(
        "--ops",
        required=True,
        help="JSON array of operations to execute",
    )
    parser.add_argument("--demo", action="store_true", help="Use demo cache if available")
    args = parser.parse_args()

    if args.demo:
        cache_path = DEMO_CACHE_DIR / f"{Path(args.input_file).name}.clean.json"
        if cache_path.exists():
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            result["_demo_cached"] = True
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return

    try:
        ops = json.loads(args.ops)
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "reason": f"Invalid JSON for --ops: {e}"}))
        sys.exit(1)

    if not isinstance(ops, list):
        print(json.dumps({"status": "error", "reason": "--ops must be a JSON array"}))
        sys.exit(1)

    result = clean_file(args.input_file, args.output_file, ops)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
