"""/api/search geocoding -- resurrected by the 4.1 business eval.

The eval caught /api/search returning zero results for EVERY query, after
which the agent guessed coordinates from model memory -- precisely the
hallucination the tool exists to prevent. Three stacked causes, each
silent: _source_sql never looked in the ready/ layout (processed/ is empty
on fresh deployments), the SQL hardcoded the OLD column names (the ready
index renamed address -> matched_address; the Binder error was swallowed
by a best-effort except), and the function closed the thread's cached
DuckDB connection on exit. These tests pin all three.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.service import search_address_payload

READY = Path("/mnt/data/Urban-Dossier/data/ready")
pytestmark = pytest.mark.skipif(
    not (READY / "location" / "location_index.parquet").exists(),
    reason="ready location index not present",
)


def test_street_name_returns_geocoded_hits():
    out = search_address_payload("BROADWAY", limit=3)
    assert out["results"], "BROADWAY must match in an 857k-address index"
    hit = out["results"][0]
    assert hit["address"] and hit["borough"]
    assert 40.4 < hit["latitude"] < 41.0
    assert -74.3 < hit["longitude"] < -73.6


def test_place_name_substring_matches():
    out = search_address_payload("Union Square", limit=3)
    assert out["results"]
    assert any("UNION SQUARE" in hit["address"].upper() for hit in out["results"])


def test_case_insensitive_and_limit_respected():
    out = search_address_payload("fordham", limit=2)
    assert 1 <= len(out["results"]) <= 2


def test_search_does_not_poison_the_thread_connection():
    """The old finally: con.close() broke every later query on the thread."""
    from urban_dossier_backend.providers.direct_provider import (
        DirectQueryDataProvider,
    )

    search_address_payload("BROADWAY", limit=1)
    con = DirectQueryDataProvider()._connect()
    assert con.execute("SELECT 1").fetchone()[0] == 1


def test_no_results_is_empty_not_error():
    out = search_address_payload("ZZZZ NOWHERE XYZZY", limit=3)
    assert out == {"results": []}
