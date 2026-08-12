"""Socrata discovery traversal and freshness watermarks -- EXPANSION_PLAN 3.5.

Two jobs, both read-only:

1. Walk the Discovery API for data.cityofnewyork.us (~3,000 assets) and land
   the full catalog locally -- id, name, type, category, upstream update
   time -- so dataset discovery is a local query instead of a network call.
2. Compare every locally snapshotted dataset's retrieval time against the
   upstream ``updatedAt`` from that catalog, and write a freshness index:
   which of our snapshots the city has since moved past, by how many days.

Deliberately NOT here: downloading. The existing snapshot downloader owns
pagination, checkpoints and quarantine; this module only tells it (and us)
what is stale. Publish/quarantine transactions stay with the promotion flow.

The Discovery client takes an injectable ``fetcher`` so tests exercise the
scroll pagination and merge logic against fixture pages with no network; the
live entrypoint passes a urllib-based fetcher with backoff. Scroll (not
offset) pagination, per the API docs the plan's own research captured --
offsets past 10k silently truncate, which is exactly the kind of quiet lie
this codebase keeps finding.

Usage:
    python scripts/socrata_catalog_sync.py [--domain data.cityofnewyork.us]
        [--out-dir /mnt/data/urban-dossier-state/catalog] [--limit-pages N]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

DISCOVERY_URL = "https://api.us.socrata.com/api/catalog/v1"
DEFAULT_DOMAIN = "data.cityofnewyork.us"
DEFAULT_OUT = Path("/mnt/data/urban-dossier-state/catalog")
EXPANSION_MANIFESTS = Path("/mnt/data/urban-dossier-state/datasets/raw-expansion")
PAGE_SIZE = 200

Fetcher = Callable[[str], dict]


def default_fetcher(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": "UrbanDossier/1.0 catalog-sync"}
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(30 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def normalize(asset: dict) -> dict:
    resource = asset.get("resource", {})
    classification = asset.get("classification", {})
    return {
        "id": resource.get("id"),
        "name": resource.get("name"),
        "type": resource.get("type"),
        "updated_at": resource.get("updatedAt"),
        "data_updated_at": resource.get("data_updated_at") or resource.get("dataUpdatedAt"),
        "category": classification.get("domain_category"),
        "columns": len(resource.get("columns_name") or []),
        "provenance": resource.get("provenance"),
    }


def fetch_catalog(
    fetcher: Fetcher,
    domain: str = DEFAULT_DOMAIN,
    page_size: int = PAGE_SIZE,
    limit_pages: int | None = None,
) -> list[dict]:
    """Scroll the whole domain. Terminates when a page comes back short."""
    assets: list[dict] = []
    # Scroll from the lowest possible id. The first request must ALSO be a
    # scroll request: without scroll_id the API returns relevance order, and
    # taking that page's last id as the boundary silently skips every asset
    # whose id sorts before it -- measured live as 2,688 of 3,014 assets,
    # with the Heat Vulnerability Index among the dropped.
    scroll_id = "0000-0000"
    pages = 0
    total_reported = None
    while True:
        params = {"domains": domain, "limit": str(page_size), "scroll_id": scroll_id}
        payload = fetcher(f"{DISCOVERY_URL}?{urllib.parse.urlencode(params)}")
        if total_reported is None:
            total_reported = payload.get("resultSetSize")
        page = [normalize(a) for a in payload.get("results", [])]
        assets.extend(page)
        pages += 1
        if len(page) < page_size:
            break
        if limit_pages is not None and pages >= limit_pages:
            break
        scroll_id = page[-1]["id"]
    # The scroll seam can duplicate its boundary row; dedupe on id, last wins.
    unique = list({a["id"]: a for a in assets if a["id"]}.values())
    if (
        limit_pages is None
        and total_reported is not None
        and len(unique) < 0.97 * total_reported
    ):
        raise SystemExit(
            f"catalog walk returned {len(unique)} assets but the API reports "
            f"{total_reported}; refusing to publish a silently truncated catalog"
        )
    return unique


def tracked_snapshots(manifest_root: Path = EXPANSION_MANIFESTS) -> list[dict]:
    """Every locally snapshotted dataset that recorded its identity.

    Only manifests carrying a dataset_id are trackable; core raw CSVs that
    predate manifests are reported as untracked rather than guessed at --
    inventing 4x4 ids here would poison the freshness report with lies.
    """
    out = []
    for manifest in sorted(manifest_root.glob("*/manifest.json")):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        dataset_id = data.get("dataset_id")
        if not dataset_id or "/" in str(dataset_id):
            continue
        out.append(
            {
                "id": dataset_id,
                "snapshot": manifest.parent.name,
                "retrieved": data.get("retrieval_timestamp"),
                "complete": data.get("complete_or_sample"),
            }
        )
    return out


def _parse_when(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if text.isdigit():  # epoch seconds
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def freshness(catalog: Iterable[dict], tracked: list[dict]) -> list[dict]:
    by_id = {a["id"]: a for a in catalog}
    report = []
    for snap in tracked:
        upstream = by_id.get(snap["id"])
        upstream_at = _parse_when(
            (upstream or {}).get("data_updated_at") or (upstream or {}).get("updated_at")
        )
        local_at = _parse_when(snap.get("retrieved"))
        stale_days: float | None = None
        if upstream_at and local_at and upstream_at > local_at:
            stale_days = round((upstream_at - local_at).total_seconds() / 86400, 1)
        report.append(
            {
                **snap,
                "upstream_name": (upstream or {}).get("name"),
                "upstream_updated": upstream_at.isoformat() if upstream_at else None,
                "status": (
                    "missing_upstream" if upstream is None
                    else "stale" if stale_days is not None
                    else "fresh"
                ),
                "stale_days": stale_days,
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit-pages", type=int, default=None)
    args = parser.parse_args()

    catalog = fetch_catalog(default_fetcher, args.domain, limit_pages=args.limit_pages)
    tracked = tracked_snapshots()
    report = freshness(catalog, tracked)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = args.out_dir / "socrata_catalog.jsonl"
    tmp = catalog_path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        for asset in catalog:
            fh.write(json.dumps(asset) + "\n")
    tmp.replace(catalog_path)

    freshness_path = args.out_dir / "freshness.json"
    tmp = freshness_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "domain": args.domain,
        "catalog_assets": len(catalog),
        "tracked_snapshots": len(tracked),
        "report": report,
    }, indent=2) + "\n")
    tmp.replace(freshness_path)

    print(f"catalog: {len(catalog):,} assets -> {catalog_path}")
    for row in report:
        print(f"  {row['status']:16} {row['id']:11} {row['snapshot']:16} "
              f"stale_days={row['stale_days']}")


if __name__ == "__main__":
    main()
