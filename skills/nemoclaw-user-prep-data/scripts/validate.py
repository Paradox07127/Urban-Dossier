#!/usr/bin/env python3
"""
Post-cleaning validation script for nemoclaw-user-prep-data (pandas, CSV-only).

Compares original and cleaned CSV files to detect:
- Row count changes
- Column additions/removals
- Null rate changes per column
- Unique value count changes per column
- New problems introduced by cleaning (e.g., new nulls from type casting)

Does NOT judge whether changes are good or bad — only reports the diff.

Usage:
    python validate.py <original.csv> <cleaned.csv> [--output <report.json>] [--demo]
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd


DEMO_CACHE_DIR = Path(__file__).parent / "demo_cache"


def _detect_format(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext != ".csv":
        raise ValueError(
            f"Unsupported file format: {ext}. This skill only supports CSV. "
            f"Convert your file to CSV first."
        )
    return "csv"


def _load(path: str) -> pd.DataFrame:
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


def _dtype_str(series: pd.Series) -> str:
    name = str(series.dtype)
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


def _column_stats(df: pd.DataFrame) -> dict:
    total_rows = len(df)
    out = {}
    for col in df.columns:
        s = df[col]
        null_count = int(s.isna().sum())
        out[col] = {
            "dtype": _dtype_str(s),
            "null_count": null_count,
            "null_rate": round(null_count / total_rows, 4) if total_rows > 0 else 0.0,
            "n_unique": int(s.nunique(dropna=True)),
        }
    return out


def validate(original_path: str, cleaned_path: str) -> dict:
    original_path = os.path.abspath(original_path)
    cleaned_path = os.path.abspath(cleaned_path)

    try:
        orig_df = _load(original_path)
        clean_df = _load(cleaned_path)
    except Exception as e:
        return {
            "status": "error",
            "original_file": original_path,
            "cleaned_file": cleaned_path,
            "reason": f"{type(e).__name__}: {e}",
        }

    orig_rows = len(orig_df)
    clean_rows = len(clean_df)
    orig_stats = _column_stats(orig_df)
    clean_stats = _column_stats(clean_df)

    orig_cols = set(orig_stats.keys())
    clean_cols = set(clean_stats.keys())

    removed_columns = sorted(orig_cols - clean_cols)
    added_columns = sorted(clean_cols - orig_cols)
    common_columns = sorted(orig_cols & clean_cols)

    column_changes = []
    new_problems: list[dict] = []

    for col in common_columns:
        o = orig_stats[col]
        c = clean_stats[col]

        change = {
            "column": col,
            "type_before": o["dtype"],
            "type_after": c["dtype"],
            "type_changed": o["dtype"] != c["dtype"],
            "null_rate_before": o["null_rate"],
            "null_rate_after": c["null_rate"],
            "null_rate_delta": round(c["null_rate"] - o["null_rate"], 4),
            "n_unique_before": o["n_unique"],
            "n_unique_after": c["n_unique"],
        }
        column_changes.append(change)

        if c["null_rate"] > o["null_rate"] and o["null_rate"] < 0.01:
            new_problems.append({
                "type": "new_nulls_introduced",
                "column": col,
                "detail": f"Null rate increased from {o['null_rate']:.2%} to {c['null_rate']:.2%}",
                "severity": "high",
            })
        if c["null_rate"] > o["null_rate"] + 0.1:
            new_problems.append({
                "type": "significant_null_increase",
                "column": col,
                "detail": f"Null rate jumped by {c['null_rate'] - o['null_rate']:.2%}",
                "severity": "medium",
            })
        if c["n_unique"] == 1 and o["n_unique"] > 1:
            new_problems.append({
                "type": "collapsed_to_constant",
                "column": col,
                "detail": f"Column went from {o['n_unique']} unique values to 1",
                "severity": "high",
            })

    return {
        "status": "ok",
        "original_file": original_path,
        "cleaned_file": cleaned_path,
        "row_count_before": orig_rows,
        "row_count_after": clean_rows,
        "row_count_delta": clean_rows - orig_rows,
        "columns_before": len(orig_cols),
        "columns_after": len(clean_cols),
        "removed_columns": removed_columns,
        "added_columns": added_columns,
        "column_changes": column_changes,
        "new_problems_detected": new_problems,
        "has_new_problems": len(new_problems) > 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate cleaned CSV against original (pandas)")
    parser.add_argument("original_file", help="Path to the original CSV file")
    parser.add_argument("cleaned_file", help="Path to the cleaned CSV file")
    parser.add_argument("--output", "-o", help="Output JSON path (default: stdout)")
    parser.add_argument("--demo", action="store_true", help="Use demo cache if available")
    args = parser.parse_args()

    if args.demo:
        cache_path = DEMO_CACHE_DIR / f"{Path(args.original_file).name}.validation.json"
        if cache_path.exists():
            result = json.loads(cache_path.read_text(encoding="utf-8"))
            result["_demo_cached"] = True
            output = json.dumps(result, indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(output, encoding="utf-8")
            else:
                print(output)
            return

    result = validate(args.original_file, args.cleaned_file)
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)

    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
