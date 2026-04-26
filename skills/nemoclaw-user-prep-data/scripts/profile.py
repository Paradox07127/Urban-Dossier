#!/usr/bin/env python3
"""
Data profiling script for nemoclaw-user-prep-data (pandas, CSV-only).

Reads a single CSV file and outputs a structured JSON profile.
Uses pandas + numpy + (optionally) chardet for encoding sniffing.
Does NOT make decisions or recommendations — only reports facts.

What this version adds vs the DuckDB version
---------------------------------------------
1. CSV-only loader (no Excel/Parquet/JSON branches).
2. **Value-level quality scanner (Patch A)**: detects mixed boolean
   encodings, mixed date formats, invalid dates, casing inconsistency,
   leading/trailing whitespace, exact-row duplicates, lat/lon out of
   bounds, negative-where-positive-expected, suspicious zeros, and
   numeric-stored-as-string. The original profiler only caught
   structural issues (empty/constant/high-null/dup-cols/bad-name).
   These value-level issues are appended to the same `quality_issues`
   list, with a `severity` and `examples` field.

The output JSON schema is a strict superset of the DuckDB version's,
so any downstream code that read the old fields keeps working.

Usage:
    python profile.py <file_path> [--output <out.json>] [--sample-limit N] [--demo]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_LIMIT_DEFAULT = None  # Full read by default. pandas loads to memory.
SAMPLE_VALUES_COUNT = 5
EXAMPLE_ROW_COUNT = 5         # how many example row indices to surface per issue
DEMO_CACHE_DIR = Path(__file__).parent / "demo_cache"

# Heuristic vocabularies for the value-level scanner
BOOLEAN_TRUE_TOKENS = {"y", "yes", "true", "t", "1", "on"}
BOOLEAN_FALSE_TOKENS = {"n", "no", "false", "f", "0", "off"}
BOOLEAN_TOKENS = BOOLEAN_TRUE_TOKENS | BOOLEAN_FALSE_TOKENS

# Common date format regexes (each must match a *full* trimmed string)
DATE_FORMAT_PATTERNS = {
    "iso_date":      re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "iso_datetime":  re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$"),
    "us_slash":      re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),
    "us_dash":       re.compile(r"^\d{1,2}-\d{1,2}-\d{2,4}$"),
    "eu_slash":      re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    "month_word":    re.compile(r"^\d{1,2}[-\s][A-Za-z]{3,}[-\s]\d{2,4}$"),
}

# Column-name hints used to decide whether negatives are expected, etc.
POSITIVE_EXPECTED_HINTS = (
    "price", "amount", "cost", "fee", "qty", "quantity", "count",
    "rate", "size", "duration", "weight", "height", "length", "age",
    "rev", "revenue", "income", "balance",
)

LAT_HINTS = ("lat", "latitude")
LON_HINTS = ("lon", "lng", "longitude")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _detect_format(path: str) -> str:
    """CSV-only profile. Anything else is rejected at the boundary."""
    ext = Path(path).suffix.lower()
    if ext != ".csv":
        raise ValueError(
            f"Unsupported file format: {ext}. This skill only supports CSV. "
            f"Convert your file to CSV first."
        )
    return "csv"


def _file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


# File-size threshold (in megabytes) above which the cuDF fast-path is tried.
# Below this, pandas is faster because GPU upload overhead dominates.
CUDF_MIN_FILE_SIZE_MB = 100.0


def _try_read_csv_with_cudf(
    path: str, sample_limit: int | None, encoding: str
) -> pd.DataFrame | None:
    """Optional cuDF accelerated read. Returns ``None`` when cuDF is
    unavailable or the read fails for any reason; the caller then falls back
    to pandas.

    cuDF is imported lazily inside this function so the surrounding module
    still loads on a developer Mac where RAPIDS is not installed.
    """

    try:
        import cudf  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        nrows = sample_limit if (sample_limit and sample_limit > 0) else None
        gdf = cudf.read_csv(
            path,
            nrows=nrows,
            encoding=encoding,
            na_values=["", "NA", "N/A", "n/a", "null", "NULL", "None"],
        )
        # Hand the data back to pandas for the rest of the pipeline so the
        # value-level scanner (which depends on pandas-only ops like
        # ``Series.str.match`` against a Python regex) sees a familiar object.
        return gdf.to_pandas()
    except Exception:  # noqa: BLE001 - any cuDF failure -> pandas fallback
        return None


def _read_csv(path: str, sample_limit: int | None) -> tuple[pd.DataFrame, str]:
    """
    Load a CSV into a pandas DataFrame. Falls back through latin-1 if UTF-8
    sniffing fails. Returns (df, warning) - warning is non-empty when
    sampling was applied.

    For files larger than ``CUDF_MIN_FILE_SIZE_MB``, attempts cuDF
    (`cudf.read_csv`) first and converts the result to pandas. This is a
    best-effort fast-path; any failure (cuDF not installed, GPU OOM, parser
    quirk) falls back silently to the pandas path below.
    """
    nrows = sample_limit if (sample_limit and sample_limit > 0) else None

    use_cudf = False
    try:
        use_cudf = _file_size_mb(path) >= CUDF_MIN_FILE_SIZE_MB
    except OSError:
        use_cudf = False

    last_err = None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            df: pd.DataFrame | None = None
            if use_cudf:
                df = _try_read_csv_with_cudf(path, sample_limit, encoding)
            if df is None:
                df = pd.read_csv(
                    path,
                    nrows=nrows,
                    encoding=encoding,
                    low_memory=False,           # faster type inference + no chunked dtype mismatch
                    keep_default_na=True,
                    na_values=["", "NA", "N/A", "n/a", "null", "NULL", "None"],
                )
            warning = (
                f"Explicit sample_limit={sample_limit:,} applied — "
                f"full-scan statistics suppressed."
                if nrows is not None else ""
            )
            return df, warning
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise UnicodeDecodeError("utf-8", b"", 0, 0, str(last_err) if last_err else "encoding")


# ---------------------------------------------------------------------------
# Type / role inference
# ---------------------------------------------------------------------------

def _dtype_str(series: pd.Series) -> str:
    """Stringified dtype for the JSON output. Mirrors DuckDB-ish naming."""
    dt = series.dtype
    name = str(dt)
    # Normalise common pandas names to a more SQL-ish vocabulary
    mapping = {
        "object": "VARCHAR",
        "string": "VARCHAR",
        "int8": "TINYINT", "int16": "SMALLINT", "int32": "INTEGER", "int64": "BIGINT",
        "uint8": "UTINYINT", "uint16": "USMALLINT", "uint32": "UINTEGER", "uint64": "UBIGINT",
        "float16": "FLOAT", "float32": "FLOAT", "float64": "DOUBLE",
        "bool": "BOOLEAN",
        "datetime64[ns]": "TIMESTAMP",
    }
    return mapping.get(name, name.upper())


def _infer_semantic_role(
    col_name: str,
    dtype: str,
    null_rate: float,
    n_unique: int,
    total_rows: int,
    sample_values: list,
) -> str:
    """
    Heuristic-based semantic role inference.
    Returns: identifier | date | category | measure | geographic | text | unknown
    """
    name = col_name.lower().strip()

    date_keywords = [
        "date", "time", "timestamp", "created", "updated", "modified",
        "dt", "year", "month", "day",
    ]
    if any(kw in name for kw in date_keywords) or "date" in dtype.lower() or "timestamp" in dtype.lower():
        return "date"

    id_keywords = ["id", "key", "code", "uuid", "guid", "number", "num", "no", "index", "pk"]
    if any(name == kw or name.endswith(f"_{kw}") or name.startswith(f"{kw}_") for kw in id_keywords):
        if n_unique > 0 and n_unique / max(total_rows, 1) > 0.9:
            return "identifier"

    geo_keywords = [
        "lat", "lng", "lon", "longitude", "latitude", "zip", "zipcode",
        "postal", "city", "state", "country", "borough", "boro",
        "address", "street", "county", "region", "geo", "coord",
    ]
    if any(kw in name for kw in geo_keywords):
        return "geographic"

    if n_unique > 0 and n_unique < 50 and total_rows > 100:
        if "varchar" in dtype.lower() or "char" in dtype.lower():
            return "category"

    numeric_types = [
        "integer", "bigint", "double", "float", "decimal", "numeric",
        "real", "smallint", "tinyint", "hugeint",
    ]
    if any(t in dtype.lower() for t in numeric_types):
        if n_unique > 20 or total_rows <= 20:
            return "measure"
        return "category"

    if "varchar" in dtype.lower() or "char" in dtype.lower():
        avg_len = 0
        non_null = [v for v in sample_values if v is not None]
        for v in non_null:
            avg_len += len(str(v))
        avg_len = avg_len / max(len(non_null), 1)
        if avg_len > 50:
            return "text"
        if n_unique > 0 and n_unique / max(total_rows, 1) > 0.5:
            return "text"
        return "category"

    return "unknown"


# ---------------------------------------------------------------------------
# Structural quality issues (same as DuckDB version)
# ---------------------------------------------------------------------------

def _detect_structural_issues(columns_info: list[dict]) -> list[dict]:
    """5 column-level structural checks. Same as the original DuckDB profiler."""
    issues: list[dict] = []

    for col in columns_info:
        name = col["name"]
        null_rate = col["null_rate"]
        n_unique = col["n_unique"]

        if null_rate > 0.5:
            issues.append({
                "type": "high_null_rate",
                "column": name,
                "detail": f"{null_rate:.1%} null values",
                "severity": "high" if null_rate > 0.8 else "medium",
            })
        if null_rate >= 1.0:
            issues.append({
                "type": "empty_column",
                "column": name,
                "detail": "Column is 100% null",
                "severity": "high",
            })
        if n_unique == 1 and null_rate < 1.0:
            sv = col.get("sample_values") or ["?"]
            issues.append({
                "type": "constant_column",
                "column": name,
                "detail": f"Only one unique value: {sv[0]}",
                "severity": "low",
            })

    for i, col_a in enumerate(columns_info):
        for col_b in columns_info[i + 1:]:
            if (col_a["n_unique"] == col_b["n_unique"]
                    and col_a["n_unique"] > 0
                    and col_a["sample_values"] == col_b["sample_values"]):
                issues.append({
                    "type": "suspected_duplicate_columns",
                    "columns": [col_a["name"], col_b["name"]],
                    "detail": "Same unique count and identical sample values",
                    "severity": "medium",
                })

    for col in columns_info:
        name = col["name"]
        if " " in name or any(c in name for c in "!@#$%^&*()+=[]{}|;:'\",<>?/\\"):
            issues.append({
                "type": "problematic_column_name",
                "column": name,
                "detail": "Column name contains spaces or special characters",
                "severity": "low",
            })

    return issues


# ---------------------------------------------------------------------------
# Patch A: Value-level quality scanner
# ---------------------------------------------------------------------------

def _is_string_dtype(s: pd.Series) -> bool:
    return s.dtype == object or pd.api.types.is_string_dtype(s)


def _is_numeric_dtype(s: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)


def _example_rows(mask: pd.Series, n: int = EXAMPLE_ROW_COUNT) -> list[int]:
    """Return up to n 1-based row indices where mask is True."""
    if mask is None or not mask.any():
        return []
    idx = mask[mask].index.tolist()[:n]
    # CSV row numbers are 1-based and we add 1 again to skip the header line
    return [int(i) + 1 for i in idx]


def _scan_casing_inconsistency(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_string_dtype(s):
            continue
        non_null = s.dropna().astype(str)
        if non_null.empty:
            continue
        n_unique_raw = non_null.nunique()
        n_unique_lower = non_null.str.lower().nunique()
        if n_unique_raw > n_unique_lower:
            # Find specific examples: values that collide on lower() but differ raw
            grouped = non_null.groupby(non_null.str.lower())
            colliding = grouped.filter(lambda g: g.nunique() > 1)
            sample_pairs: list[str] = []
            for _key, group in colliding.groupby(colliding.str.lower()):
                vals = sorted(set(group.tolist()))[:3]
                sample_pairs.append("|".join(vals))
                if len(sample_pairs) >= 3:
                    break
            out.append({
                "type": "casing_inconsistency",
                "column": col,
                "detail": (
                    f"Found {n_unique_raw - n_unique_lower} casing-collision groups "
                    f"(e.g. {', '.join(sample_pairs)})"
                ),
                "severity": "medium",
                "examples": sample_pairs,
            })
    return out


def _scan_whitespace_drift(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_string_dtype(s):
            continue
        non_null = s.dropna().astype(str)
        if non_null.empty:
            continue
        bad_mask = non_null != non_null.str.strip()
        bad_count = int(bad_mask.sum())
        if bad_count > 0:
            example_rows = _example_rows(bad_mask.reindex(s.index, fill_value=False))
            out.append({
                "type": "whitespace_drift",
                "column": col,
                "detail": f"{bad_count} values have leading/trailing whitespace",
                "severity": "low",
                "example_rows": example_rows,
            })
    return out


def _scan_mixed_date_formats(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_string_dtype(s):
            continue
        non_null = s.dropna().astype(str).str.strip()
        if non_null.empty:
            continue
        formats_seen: dict[str, int] = {}
        for fmt_name, pat in DATE_FORMAT_PATTERNS.items():
            n = int(non_null.str.match(pat).sum())
            if n > 0:
                formats_seen[fmt_name] = n
        # Only flag if 2+ different formats AND together they cover >25% of rows
        if len(formats_seen) >= 2 and sum(formats_seen.values()) > 0.25 * len(non_null):
            out.append({
                "type": "mixed_date_formats",
                "column": col,
                "detail": "Multiple date formats present: " + ", ".join(
                    f"{k}({v})" for k, v in formats_seen.items()
                ),
                "severity": "high",
                "formats": formats_seen,
            })
    return out


def _scan_invalid_dates(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_string_dtype(s):
            continue
        non_null = s.dropna().astype(str).str.strip()
        if non_null.empty:
            continue
        # Only check columns where >=50% look like dates by some pattern
        any_date_mask = pd.Series(False, index=non_null.index)
        for pat in DATE_FORMAT_PATTERNS.values():
            any_date_mask = any_date_mask | non_null.str.match(pat)
        if any_date_mask.sum() < 0.5 * len(non_null):
            continue
        # Now try to parse with pandas; values that fail and don't look like a date
        # are likely garbage rather than just non-dates.
        parsed = pd.to_datetime(non_null, errors="coerce", format="mixed")
        bad_mask = parsed.isna() & any_date_mask
        bad_count = int(bad_mask.sum())
        if bad_count > 0:
            example_rows = _example_rows(bad_mask.reindex(s.index, fill_value=False))
            example_vals = non_null[bad_mask].head(3).tolist()
            out.append({
                "type": "invalid_dates",
                "column": col,
                "detail": (
                    f"{bad_count} values look like dates but don't parse "
                    f"(e.g. {', '.join(map(str, example_vals))})"
                ),
                "severity": "high",
                "example_rows": example_rows,
            })
    return out


def _scan_mixed_boolean_encoding(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_string_dtype(s):
            continue
        non_null = s.dropna().astype(str).str.strip()
        if non_null.empty:
            continue
        if non_null.nunique() > 10:
            continue  # too cardinal to be a boolean
        token_set = set(non_null.str.lower().unique())
        boolean_hits = token_set & BOOLEAN_TOKENS
        # Mixed encoding = at least 3 distinct boolean tokens (e.g. Y, yes, TRUE)
        if len(boolean_hits) >= 3:
            out.append({
                "type": "mixed_boolean_encoding",
                "column": col,
                "detail": (
                    f"Column looks boolean but uses {len(boolean_hits)} different "
                    f"encodings: {sorted(boolean_hits)}"
                ),
                "severity": "high",
                "tokens": sorted(boolean_hits),
            })
    return out


def _scan_numeric_stored_as_string(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_string_dtype(s):
            continue
        non_null = s.dropna().astype(str).str.strip()
        if non_null.empty:
            continue
        # Strip thousands separators before parsing
        cleaned = non_null.str.replace(",", "", regex=False)
        coerced = pd.to_numeric(cleaned, errors="coerce")
        coerce_rate = coerced.notna().mean()
        # Either: ALL values look numeric (column should have been numeric)
        # OR: 70%+ are numeric and the rest is garbage
        if coerce_rate >= 0.95:
            out.append({
                "type": "numeric_stored_as_string",
                "column": col,
                "detail": (
                    f"{coerce_rate:.0%} of values are numeric. Column should "
                    f"probably be cast to a numeric type."
                ),
                "severity": "medium",
            })
        elif coerce_rate >= 0.70:
            bad_mask = coerced.isna()
            example_rows = _example_rows(bad_mask.reindex(s.index, fill_value=False))
            example_vals = non_null[bad_mask].head(3).tolist()
            out.append({
                "type": "mixed_numeric_string",
                "column": col,
                "detail": (
                    f"{coerce_rate:.0%} numeric, rest is non-numeric "
                    f"(e.g. {', '.join(map(str, example_vals))})"
                ),
                "severity": "high",
                "example_rows": example_rows,
            })
    return out


def _scan_negatives_unexpected(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_numeric_dtype(s):
            continue
        name = col.lower()
        if not any(h in name for h in POSITIVE_EXPECTED_HINTS):
            continue
        bad_mask = s < 0
        bad_count = int(bad_mask.sum())
        if bad_count > 0:
            example_rows = _example_rows(bad_mask)
            out.append({
                "type": "negative_value_unexpected",
                "column": col,
                "detail": (
                    f"{bad_count} negative value(s) in a column where positives "
                    f"are expected (column name: {col!r})"
                ),
                "severity": "high",
                "example_rows": example_rows,
            })
    return out


def _scan_suspicious_zeros(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_numeric_dtype(s):
            continue
        name = col.lower()
        if not any(h in name for h in POSITIVE_EXPECTED_HINTS):
            continue
        non_null = s.dropna()
        if non_null.empty:
            continue
        zeros = (non_null == 0).sum()
        zero_rate = zeros / len(non_null)
        # Only flag when zeros are minority but non-trivial — avoid noise on
        # naturally-zero columns like a "discount" field.
        if 0 < zero_rate < 0.5 and zeros >= 1:
            zero_mask = (s == 0)
            example_rows = _example_rows(zero_mask)
            out.append({
                "type": "suspicious_zero",
                "column": col,
                "detail": (
                    f"{zeros} zero value(s) ({zero_rate:.0%}) in a column where "
                    f"zeros are unusual — possible placeholders for missing data"
                ),
                "severity": "medium",
                "example_rows": example_rows,
            })
    return out


def _scan_lat_lon_bounds(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        s = df[col]
        if not _is_numeric_dtype(s):
            continue
        name = col.lower()
        if any(h == name or name.endswith(f"_{h}") or h in name for h in LAT_HINTS):
            bad_mask = s.notna() & ((s < -90) | (s > 90))
            if bad_mask.any():
                out.append({
                    "type": "geographic_out_of_bounds",
                    "column": col,
                    "detail": f"Latitude values outside [-90, 90]: {int(bad_mask.sum())} rows",
                    "severity": "high",
                    "example_rows": _example_rows(bad_mask),
                })
        elif any(h == name or name.endswith(f"_{h}") or h in name for h in LON_HINTS):
            bad_mask = s.notna() & ((s < -180) | (s > 180))
            if bad_mask.any():
                out.append({
                    "type": "geographic_out_of_bounds",
                    "column": col,
                    "detail": f"Longitude values outside [-180, 180]: {int(bad_mask.sum())} rows",
                    "severity": "high",
                    "example_rows": _example_rows(bad_mask),
                })
    return out


def _scan_exact_row_duplicates(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    dup_mask = df.duplicated(keep=False)
    n_dup = int(dup_mask.sum())
    if n_dup == 0:
        return []
    return [{
        "type": "exact_row_duplicates",
        "detail": f"{n_dup} rows participate in exact-duplicate groups",
        "severity": "high",
        "example_rows": _example_rows(dup_mask),
    }]


def _scan_id_collision(df: pd.DataFrame, columns_info: list[dict]) -> list[dict]:
    """For columns the role-inference flagged as identifier, find duplicate IDs."""
    out = []
    id_cols = [c["name"] for c in columns_info if c.get("semantic_role") == "identifier"]
    for col in id_cols:
        if col not in df.columns:
            continue
        s = df[col]
        non_null = s.dropna()
        if non_null.empty:
            continue
        n_dup = int(non_null.duplicated(keep=False).sum())
        if n_dup > 0:
            dup_mask = s.notna() & s.duplicated(keep=False)
            out.append({
                "type": "duplicate_identifier",
                "column": col,
                "detail": (
                    f"Identifier column has {n_dup} rows sharing IDs with another "
                    f"row (the column should be unique by name)"
                ),
                "severity": "high",
                "example_rows": _example_rows(dup_mask),
            })
    return out


def _detect_value_level_issues(df: pd.DataFrame, columns_info: list[dict]) -> list[dict]:
    """Patch A: run all value-level scanners and concatenate the issues."""
    issues: list[dict] = []
    issues.extend(_scan_casing_inconsistency(df))
    issues.extend(_scan_whitespace_drift(df))
    issues.extend(_scan_mixed_date_formats(df))
    issues.extend(_scan_invalid_dates(df))
    issues.extend(_scan_mixed_boolean_encoding(df))
    issues.extend(_scan_numeric_stored_as_string(df))
    issues.extend(_scan_negatives_unexpected(df))
    issues.extend(_scan_suspicious_zeros(df))
    issues.extend(_scan_lat_lon_bounds(df))
    issues.extend(_scan_exact_row_duplicates(df))
    issues.extend(_scan_id_collision(df, columns_info))
    return issues


# ---------------------------------------------------------------------------
# Main profiling logic
# ---------------------------------------------------------------------------

def _serialize(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, (np.integer, np.floating, np.bool_)):
        return v.item()
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    return str(v)


def _column_info(col_name: str, series: pd.Series, total_rows: int) -> dict:
    null_count = int(series.isna().sum())
    null_rate = null_count / total_rows if total_rows > 0 else 0.0
    n_unique = int(series.nunique(dropna=True))

    try:
        non_null = series.dropna()
        min_val = non_null.min() if not non_null.empty else None
        max_val = non_null.max() if not non_null.empty else None
    except (TypeError, ValueError):
        min_val = None
        max_val = None

    try:
        sample_values = (
            series.dropna().drop_duplicates().head(SAMPLE_VALUES_COUNT).tolist()
        )
    except Exception:
        sample_values = []
    sample_values = [_serialize(v) for v in sample_values]
    min_val = _serialize(min_val)
    max_val = _serialize(max_val)

    dtype_str = _dtype_str(series)
    semantic_role = _infer_semantic_role(
        col_name, dtype_str, null_rate, n_unique, total_rows, sample_values
    )

    return {
        "name": col_name,
        "type": dtype_str,
        "null_count": null_count,
        "null_rate": round(null_rate, 4),
        "n_unique": n_unique,
        "min": min_val,
        "max": max_val,
        "sample_values": sample_values,
        "semantic_role": semantic_role,
    }


def profile_file(file_path: str, sample_limit: int | None = SAMPLE_LIMIT_DEFAULT) -> dict:
    path = os.path.abspath(file_path)
    if not os.path.exists(path):
        return {"status": "error", "file": path, "reason": f"File not found: {path}"}

    try:
        fmt = _detect_format(path)
    except ValueError as e:
        return {"status": "error", "file": path, "reason": str(e)}

    try:
        df, warning = _read_csv(path, sample_limit)
    except Exception as e:
        return {
            "status": "error",
            "file": path,
            "reason": f"{type(e).__name__}: {e}",
        }

    total_rows = len(df)
    columns_info = [_column_info(col, df[col], total_rows) for col in df.columns]
    structural_issues = _detect_structural_issues(columns_info)
    value_level_issues = _detect_value_level_issues(df, columns_info)
    quality_issues = structural_issues + value_level_issues

    return {
        "status": "ok",
        "file": path,
        "format": fmt,
        "file_size_mb": round(_file_size_mb(path), 2),
        "total_rows": total_rows,
        "total_columns": len(columns_info),
        "sampled": warning != "",
        "sampling_warning": warning if warning else None,
        "columns": columns_info,
        "quality_issues": quality_issues,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _demo_cache_path(file_path: str) -> Path:
    return DEMO_CACHE_DIR / f"{Path(file_path).name}.profile.json"


def main():
    parser = argparse.ArgumentParser(description="Profile a CSV data file (pandas)")
    parser.add_argument("file_path", help="Path to the CSV file")
    parser.add_argument("--output", "-o", help="Output JSON path (default: stdout)")
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=None,
        help=(
            "Explicit row cap for testing or for files too large to fit in "
            "memory. Omit for full read (required for duplicate / ID / "
            "referential checks in Phase 2)."
        ),
    )
    parser.add_argument("--demo", action="store_true", help="Use demo cache if available")
    args = parser.parse_args()

    if args.demo:
        cache_path = _demo_cache_path(args.file_path)
        if cache_path.exists():
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            result["_demo_cached"] = True
            output = json.dumps(result, indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(output, encoding="utf-8")
            else:
                print(output)
            return

    result = profile_file(args.file_path, sample_limit=args.sample_limit)
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
