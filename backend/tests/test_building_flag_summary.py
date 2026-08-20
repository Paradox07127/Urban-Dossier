"""Building flag labels must not end on a preposition.

The compact ready layer drops display-only address columns on purpose, so
``HouseNumber``/``StreetName`` arrive as NULL for every row it serves. The
label builder used to append " at {house} {street}" unconditionally, which
shipped "Class B violation at" -- a sentence cut off mid-clause -- to the
inspector panel for every housing violation in the city, and "AEP building at"
for every AEP row.

These cases pin the rule rather than the wording: the preposition appears only
when something follows it, and a row with no address still gets a label that
tells it apart from its neighbours.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider

summary = DirectQueryDataProvider._flag_summary


def test_address_present_reads_as_a_sentence():
    assert summary("Class B violation", "67", "WALL STREET") == "Class B violation at 67 WALL STREET"


def test_no_address_falls_back_to_the_date_not_a_dangling_at():
    result = summary("Class B violation", None, None, "2024-03-11")
    assert result == "Class B violation · 2024-03-11"
    assert not result.endswith("at")


def test_no_address_and_no_date_still_names_the_subject():
    result = summary("AEP building", None, None, None)
    assert result == "AEP building"
    assert not result.endswith("at")


def test_blank_strings_count_as_absent():
    # DuckDB hands back "" as readily as NULL for a missing text column, and a
    # whitespace-only address must not resurrect the preposition either.
    assert summary("Class A violation", "", "   ", None) == "Class A violation"


def test_half_an_address_is_still_an_address():
    # Street with no house number is a real record, not a broken one.
    assert summary("AEP building", None, "BROADWAY", None) == "AEP building at BROADWAY"


def test_unparseable_date_does_not_leak_into_the_label():
    assert summary("Class C violation", None, None, "not-a-date") == "Class C violation"
