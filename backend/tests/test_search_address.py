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

from urban_dossier_backend.service import (
    parse_location_query,
    strip_location_qualifiers,
    search_address_payload,
)

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


# --- query parsing and matching, from the 2026-08-14 agent trace ------------
#
# The eval case "compare Union Square with Astoria" was recorded as the model
# failing to select compare_neighborhoods. The stored trajectory showed the
# opposite: it reasoned correctly, called search_address("Union Square
# Manhattan"), got nothing, and spent its whole iteration budget retrying the
# geocode. Matching the entire query as ONE substring against the address
# column is what made adding a borough return zero rows.


def test_borough_is_lifted_out_of_the_tokens():
    tokens, borough = parse_location_query("UNION SQUARE MANHATTAN")
    assert tokens == ["UNION", "SQUARE"]
    assert borough == "MANHATTAN"


def test_new_york_qualifies_manhattan_and_nyc_qualifies_nothing():
    assert parse_location_query("UNION SQUARE NEW YORK")[1] == "MANHATTAN"
    assert parse_location_query("UNION SQUARE NYC")[1] is None


def test_street_ordinals_fold_to_bare_numbers():
    """PLUTO stores "350 5 AVENUE"; everyone writes "350 5th Avenue"."""
    tokens, _ = parse_location_query("350 5TH AVENUE")
    assert tokens == ["350", "5", "AVENUE"]
    assert parse_location_query("42ND STREET")[0] == ["42", "STREET"]


def test_adding_a_borough_no_longer_empties_the_search():
    """The exact query the agent asked and got nothing for."""
    plain = search_address_payload("Union Square", limit=3)["results"]
    qualified = search_address_payload("Union Square Manhattan", limit=3)["results"]
    assert plain, "precondition: the unqualified query still works"
    assert qualified, "adding the borough must not empty the result"
    assert qualified[0]["borough"].upper() == "MANHATTAN"


def test_borough_filters_rather_than_decorates():
    out = search_address_payload("Broadway Brooklyn", limit=5)["results"]
    assert out
    assert {r["borough"].upper() for r in out} == {"BROOKLYN"}


def test_numeric_tokens_match_on_word_boundaries_not_substrings():
    """A confidently wrong coordinate is worse than no coordinate.

    LIKE '%5%' matches the 5 inside "350", so the tokens of "350 5th Avenue"
    used to resolve to "350 6 AVENUE" in Brooklyn.
    """
    out = search_address_payload("350 5th Avenue", limit=5)["results"]
    addresses = [r["address"].upper() for r in out]
    assert not [a for a in addresses if "6 AVENUE" in a or "3 AVENUE" in a]


def test_an_indexed_numbered_avenue_still_resolves():
    out = search_address_payload("350 3rd Avenue", limit=3)["results"]
    assert out, "ordinal folding must not cost us real addresses"
    assert out[0]["address"].upper() == "350 3 AVENUE"


def test_a_named_place_that_is_not_an_address_resolves():
    """An address index cannot answer "Fordham Plaza" -- the landmark
    sources already published locally can."""
    out = search_address_payload("Fordham Plaza", limit=3)["results"]
    assert out
    top = out[0]
    assert top["match_type"] != "address"
    assert 40.5 < top["latitude"] < 41.0
    assert -74.3 < top["longitude"] < -73.7


def test_landmark_ranking_prefers_the_place_over_a_business_named_after_it():
    """"UNION SQUARE EYE CARE - HARLEM" matches every token too. Ranking is
    the only thing that keeps Union Square out of Harlem."""
    out = search_address_payload("Union Square", limit=3)["results"]
    assert out
    assert "HARLEM" not in out[0]["address"].upper()


def test_a_postal_state_suffix_no_longer_empties_the_search():
    """The bug that made the agent unusable on ordinary questions.

    Every token must match, and the index stores no state abbreviation, so
    "Times Square, New York, NY" asked for an address containing TIMES,
    SQUARE and NY -- and got nothing. Models write the postal form by
    reflex, so this defeated the geocoder for any question that named a
    place, plain addresses included. Found 2026-08-22 by asking the live
    agent to compare two landmarks; it burned five search_address calls and
    honestly reported it could not resolve either.
    """
    for qualified, bare in (
        ("Times Square, New York, NY", "Times Square"),
        ("Times Square, NY", "Times Square"),
        ("67 Wall Street, New York, NY", "67 Wall Street"),
        ("Prospect Park, Brooklyn, NY", "Prospect Park, Brooklyn"),
    ):
        got = search_address_payload(qualified, limit=5)["results"]
        expected = search_address_payload(bare, limit=5)["results"]
        assert got, f"{qualified!r} returned nothing"
        assert [r["address"] for r in got] == [r["address"] for r in expected]


def test_qualifier_stripping_is_segment_anchored_not_word_removal():
    """"New York Avenue" is a real Brooklyn street.

    Dropping the words wherever they appear would delete it. Only a whole
    trailing comma segment is a qualifier.
    """
    assert strip_location_qualifiers("New York Avenue, Brooklyn") == "New York Avenue, Brooklyn"
    assert strip_location_qualifiers("Times Square, New York, NY") == "Times Square, New York"
    assert strip_location_qualifiers("Union Square, NY 10003") == "Union Square"
    assert strip_location_qualifiers("Broadway") == "Broadway"

    results = search_address_payload("New York Avenue, Brooklyn", limit=5)["results"]
    assert any("NEW YORK AVENUE" in r["address"].upper() for r in results)


def test_a_query_that_is_only_qualifiers_still_reports_no_match():
    """Stripping must not invent a match out of an empty query."""
    assert search_address_payload("New York, NY", limit=5)["results"] == []


def test_a_bare_borough_name_returns_nothing():
    """Better an honest miss than an arbitrary building in the right
    borough."""
    assert search_address_payload("Brooklyn", limit=3)["results"] == []


def test_results_declare_how_they_were_matched():
    out = search_address_payload("Union Square", limit=1)["results"]
    assert out[0]["match_type"] == "address"
