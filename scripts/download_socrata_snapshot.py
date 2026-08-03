#!/usr/bin/env python3
"""Reliably download a large Socrata dataset with keyset pagination."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def get_json(session: requests.Session, url: str, params: dict[str, str] | None = None) -> Any:
    response = session.get(url, params=params, timeout=(20, 120))
    response.raise_for_status()
    return response.json()


def write_page(
    session: requests.Session,
    url: str,
    params: dict[str, str],
    page_path: Path,
    field_names: list[str],
    key_index: int,
    previous_id: int,
    retries: int,
) -> tuple[int, int]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with session.get(url, params=params, stream=True, timeout=(20, 300)) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "csv" not in content_type.lower():
                    raise RuntimeError(f"unexpected content type: {content_type}")
                with page_path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)

            page_rows = 0
            last_id = previous_id
            with page_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle, strict=True)
                header = next(reader)
                if header != field_names:
                    raise RuntimeError(f"unexpected CSV header: {header!r}")
                for row in reader:
                    if len(row) != len(field_names):
                        raise RuntimeError(
                            f"malformed row: expected {len(field_names)} columns, got {len(row)}"
                        )
                    row_id = int(row[key_index])
                    if row_id <= last_id:
                        raise RuntimeError(f"non-increasing key: {row_id} after {last_id}")
                    last_id = row_id
                    page_rows += 1
            return page_rows, last_id
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            page_path.unlink(missing_ok=True)
            if attempt < retries:
                delay = min(5 * attempt, 30)
                print(f"[retry {attempt}/{retries}] {type(exc).__name__}: {exc}; waiting {delay}s", flush=True)
                time.sleep(delay)
    raise RuntimeError(f"page failed after {retries} attempts: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("domain", help="Socrata domain, for example data.cityofnewyork.us")
    parser.add_argument("dataset_id")
    parser.add_argument("output", type=Path)
    parser.add_argument("--order-field", required=True)
    parser.add_argument("--page-size", type=int, default=50_000)
    parser.add_argument("--retries", type=int, default=5)
    args = parser.parse_args()

    if not IDENTIFIER.fullmatch(args.order_field):
        raise SystemExit(f"invalid --order-field: {args.order_field!r}")
    if not 1 <= args.page_size <= 50_000:
        raise SystemExit("--page-size must be between 1 and 50000")

    base = f"https://{args.domain}"
    resource_url = f"{base}/resource/{args.dataset_id}.csv"
    metadata_url = f"{base}/api/views/{args.dataset_id}"
    json_url = f"{base}/resource/{args.dataset_id}.json"
    checkpoint_path = args.output.with_suffix(args.output.suffix + ".checkpoint.json")
    page_path = args.output.with_suffix(args.output.suffix + ".page")

    session = requests.Session()
    session.headers.update({"User-Agent": "Urban-Dossier-dataset-snapshot/1.0"})
    metadata = get_json(session, metadata_url)
    columns = metadata.get("columns", [])
    field_names = [column["fieldName"] for column in columns]
    display_names = [column["name"] for column in columns]
    if args.order_field not in field_names:
        raise SystemExit(f"order field {args.order_field!r} is not in dataset schema")
    key_index = field_names.index(args.order_field)

    max_payload = get_json(
        session,
        json_url,
        {"$select": f"max({args.order_field}) as max_id"},
    )
    snapshot_max = int(max_payload[0]["max_id"])
    count_payload = get_json(
        session,
        json_url,
        {
            "$select": "count(*) as count",
            "$where": f"{args.order_field} <= {snapshot_max}",
        },
    )
    expected_rows = int(count_payload[0]["count"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint["snapshot_max"] != snapshot_max:
            raise SystemExit("dataset snapshot changed; restart with a fresh output/checkpoint")
        if checkpoint.get("rows_updated_at") != metadata.get("rowsUpdatedAt"):
            raise SystemExit("dataset rows changed since the checkpoint; restart with a fresh output/checkpoint")
        if int(checkpoint["expected_rows"]) != expected_rows:
            raise SystemExit("dataset row count changed since the checkpoint; restart with a fresh output/checkpoint")
        rows_written = int(checkpoint["rows_written"])
        last_id = int(checkpoint["last_id"])
        if not args.output.exists():
            raise SystemExit("checkpoint exists but partial output is missing")
    else:
        if args.output.exists():
            raise SystemExit(f"output already exists without checkpoint: {args.output}")
        with args.output.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerow(display_names)
        rows_written = 0
        last_id = -1

    select_clause = ",".join(field_names)
    print(
        f"snapshot={args.dataset_id} rows={expected_rows} max_{args.order_field}={snapshot_max} "
        f"resume_rows={rows_written}",
        flush=True,
    )

    while rows_written < expected_rows:
        where = f"{args.order_field} > {last_id} AND {args.order_field} <= {snapshot_max}"
        params = {
            "$select": select_clause,
            "$where": where,
            "$order": f"{args.order_field} ASC",
            "$limit": str(args.page_size),
        }
        page_rows, page_last_id = write_page(
            session,
            resource_url,
            params,
            page_path,
            field_names,
            key_index,
            last_id,
            args.retries,
        )
        if page_rows == 0:
            raise RuntimeError(
                f"server returned an empty page after {rows_written}/{expected_rows} rows; "
                f"query={resource_url}?{urlencode(params)}"
            )

        with page_path.open("rb") as source, args.output.open("ab") as destination:
            source.readline()  # API field-name header; output already has display-name header.
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        rows_written += page_rows
        last_id = page_last_id
        checkpoint = {
            "dataset_id": args.dataset_id,
            "rows_updated_at": metadata.get("rowsUpdatedAt"),
            "snapshot_max": snapshot_max,
            "expected_rows": expected_rows,
            "rows_written": rows_written,
            "last_id": last_id,
        }
        checkpoint_path.write_text(
            json.dumps(checkpoint, indent=2) + "\n",
            encoding="utf-8",
        )
        page_path.unlink(missing_ok=True)
        print(
            f"[page] rows={rows_written}/{expected_rows} last_id={last_id} "
            f"size={args.output.stat().st_size}",
            flush=True,
        )

    if rows_written != expected_rows:
        raise RuntimeError(f"row count mismatch: wrote {rows_written}, expected {expected_rows}")
    final_metadata = get_json(session, metadata_url)
    if final_metadata.get("rowsUpdatedAt") != metadata.get("rowsUpdatedAt"):
        raise RuntimeError(
            "dataset rows changed during download; keep the partial file quarantined and retry"
        )
    print(f"complete: {args.output} ({rows_written} rows)", flush=True)


if __name__ == "__main__":
    main()
