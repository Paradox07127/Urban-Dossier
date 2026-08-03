#!/usr/bin/env python3
"""Atomically rewrite a Parquet tree for analytical predicate pushdown."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pyarrow.parquet as pq


def is_optimized(path: Path, compression: str, row_group_rows: int) -> bool:
    metadata = pq.ParquetFile(path).metadata
    for group_index in range(metadata.num_row_groups):
        group = metadata.row_group(group_index)
        if group.num_rows > row_group_rows:
            return False
        for column_index in range(group.num_columns):
            if group.column(column_index).compression.lower() != compression.lower():
                return False
    return True


def optimize(path: Path, compression: str, compression_level: int, row_group_rows: int) -> None:
    source = pq.ParquetFile(path)
    expected_rows = source.metadata.num_rows
    expected_schema = source.schema_arrow
    before_bytes = path.stat().st_size
    partial = path.with_suffix(path.suffix + ".part")
    partial.unlink(missing_ok=True)

    writer_options: dict[str, object] = {
        "compression": compression,
        "use_dictionary": True,
        "write_statistics": True,
    }
    if compression.lower() in {"zstd", "gzip", "brotli"}:
        writer_options["compression_level"] = compression_level

    try:
        with pq.ParquetWriter(partial, expected_schema, **writer_options) as writer:
            for batch in source.iter_batches(batch_size=row_group_rows, use_threads=True):
                writer.write_batch(batch, row_group_size=row_group_rows)

        rewritten = pq.ParquetFile(partial)
        if rewritten.metadata.num_rows != expected_rows:
            raise RuntimeError(
                f"row count changed for {path}: {expected_rows} -> {rewritten.metadata.num_rows}"
            )
        if not rewritten.schema_arrow.equals(expected_schema, check_metadata=True):
            raise RuntimeError(f"schema changed while rewriting {path}")
        os.replace(partial, path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    after_bytes = path.stat().st_size
    print(
        f"[optimized] {path} rows={expected_rows} bytes={before_bytes}->{after_bytes}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--compression", default="zstd")
    parser.add_argument("--compression-level", type=int, default=3)
    parser.add_argument("--row-group-rows", type=int, default=250_000)
    args = parser.parse_args()

    paths = sorted(args.root.rglob("*.parquet"))
    if not paths:
        raise SystemExit(f"no Parquet files found below {args.root}")
    for path in paths:
        if is_optimized(path, args.compression, args.row_group_rows):
            print(f"[skip] {path}", flush=True)
            continue
        optimize(path, args.compression, args.compression_level, args.row_group_rows)


if __name__ == "__main__":
    main()
