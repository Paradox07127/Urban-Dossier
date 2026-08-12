"""Catalog traversal and freshness math, no network -- EXPANSION_PLAN 3.5.

The fetcher is injected, so the scroll pagination, the boundary-row dedupe
and the staleness arithmetic are all pinned against fixture pages. The one
thing these tests refuse to do is guess dataset ids: a manifest without a
recorded identity stays untracked, because a freshness report built on
invented ids would lie with confidence.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

_spec = importlib.util.spec_from_file_location(
    "socrata_catalog_sync", REPO_ROOT / "scripts" / "socrata_catalog_sync.py"
)
scs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spec and scs)


def _asset(asset_id: str, name: str = "x", updated: str = "2026-08-01T00:00:00.000Z") -> dict:
    return {
        "resource": {
            "id": asset_id,
            "name": name,
            "type": "dataset",
            "updatedAt": updated,
            "columns_name": ["a", "b"],
        },
        "classification": {"domain_category": "City Government"},
    }


def _paged_fetcher(pages: list[list[dict]]):
    calls: list[str] = []

    def fetch(url: str) -> dict:
        calls.append(url)
        return {"results": pages[len(calls) - 1] if len(calls) <= len(pages) else []}

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_scroll_walks_until_a_short_page():
    pages = [
        [_asset(f"aaaa-{i:04d}") for i in range(3)],
        [_asset(f"bbbb-{i:04d}") for i in range(2)],  # short -> stop
    ]
    fetch = _paged_fetcher(pages)
    catalog = scs.fetch_catalog(fetch, page_size=3)
    assert len(catalog) == 5
    assert len(fetch.calls) == 2
    # Every request is a scroll request: the first from the floor id (the
    # bug this pins: an unscrolled first page comes back in relevance order
    # and poisons the boundary), later ones from the last row seen.
    assert "scroll_id=0000-0000" in fetch.calls[0]
    assert "scroll_id=aaaa-0002" in fetch.calls[1]


def test_boundary_duplicates_are_deduped_last_wins():
    pages = [
        [_asset("aaaa-0001"), _asset("dupe-0000", name="old"), _asset("aaaa-0003")],
        [_asset("dupe-0000", name="new")],
    ]
    catalog = scs.fetch_catalog(_paged_fetcher(pages), page_size=3)
    names = {a["id"]: a["name"] for a in catalog}
    assert len(catalog) == 3
    assert names["dupe-0000"] == "new"


def test_limit_pages_caps_the_walk():
    pages = [[_asset(f"p{p}-{i:03d}") for i in range(3)] for p in range(9)]
    catalog = scs.fetch_catalog(_paged_fetcher(pages), page_size=3, limit_pages=2)
    assert len(catalog) == 6


def test_tracked_snapshots_refuse_manifests_without_identity(tmp_path):
    good = tmp_path / "places"
    good.mkdir()
    (good / "manifest.json").write_text(json.dumps({
        "dataset_id": "cwsq-ngmh", "retrieval_timestamp": "2026-08-11T20:50:00+00:00",
        "complete_or_sample": "complete",
    }))
    anonymous = tmp_path / "legacy"
    anonymous.mkdir()
    (anonymous / "manifest.json").write_text(json.dumps({"rows": 5}))
    weird = tmp_path / "acs"
    weird.mkdir()
    (weird / "manifest.json").write_text(json.dumps({
        "dataset_id": "B01003 + B25070/summary-file",
    }))
    tracked = scs.tracked_snapshots(tmp_path)
    assert [t["id"] for t in tracked] == ["cwsq-ngmh"]


def test_freshness_flags_only_genuinely_newer_upstream():
    catalog = [
        scs.normalize(_asset("aaaa-0001", updated="2026-08-10T00:00:00.000Z")),
        scs.normalize(_asset("bbbb-0002", updated="2026-06-01T00:00:00.000Z")),
    ]
    tracked = [
        {"id": "aaaa-0001", "snapshot": "s1", "retrieved": "2026-08-01T00:00:00+00:00",
         "complete": "complete"},
        {"id": "bbbb-0002", "snapshot": "s2", "retrieved": "2026-08-01T00:00:00+00:00",
         "complete": "complete"},
        {"id": "cccc-0003", "snapshot": "s3", "retrieved": "2026-08-01T00:00:00+00:00",
         "complete": "sample"},
    ]
    report = {r["id"]: r for r in scs.freshness(catalog, tracked)}
    assert report["aaaa-0001"]["status"] == "stale"
    assert report["aaaa-0001"]["stale_days"] == 9.0
    assert report["bbbb-0002"]["status"] == "fresh"
    assert report["bbbb-0002"]["stale_days"] is None
    assert report["cccc-0003"]["status"] == "missing_upstream"


def test_epoch_and_iso_timestamps_both_parse():
    assert scs._parse_when("1754870400").year == 2025 or scs._parse_when("1754870400").year == 2026
    assert scs._parse_when("2026-08-11T20:50:00+00:00").day == 11
    assert scs._parse_when("") is None
    assert scs._parse_when("not a time") is None


def test_a_truncated_walk_refuses_to_publish():
    import pytest

    def fetch(url):
        return {"resultSetSize": 1000, "results": [_asset("aaaa-0001")]}

    with pytest.raises(SystemExit, match="silently truncated"):
        scs.fetch_catalog(fetch, page_size=200)
