"""query_dataset_rows filter shapes -- found by the 4.1 business eval.

The agent naturally reaches for {"min": a, "max": b} when asked to count
within bounds; that shape used to fall into the scalar branch and blow up
DuckDB as an HTTP 500 the model could not interpret. Ranges are now real,
other dict shapes are a structured error, and unknown keys keep their
explicit ignored_filters note (silently unfiltered rows presented as
filtered evidence is the worst outcome of the three).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.service import query_dataset_rows

READY = Path("/mnt/data/Urban-Dossier/data/ready")
pytestmark = pytest.mark.skipif(
    not READY.exists(), reason="ready tables not present"
)


def test_range_filter_applies_bounds():
    out = query_dataset_rows(
        "collisions",
        filters={"latitude": {"min": 40.72, "max": 40.74}},
        limit=50,
    )
    assert "error" not in out
    assert out["rows"], "range over a busy latitude band should match rows"
    assert all(40.72 <= row["latitude"] <= 40.74 for row in out["rows"])
    # total counts matches before limit, so a narrower band shrinks it.
    narrower = query_dataset_rows(
        "collisions", filters={"latitude": {"min": 40.72, "max": 40.73}}, limit=1
    )
    assert narrower["total"] <= out["total"]


def test_min_only_and_max_only_are_valid():
    lo = query_dataset_rows(
        "collisions", filters={"latitude": {"min": 40.9}}, limit=10
    )
    assert "error" not in lo
    assert all(row["latitude"] >= 40.9 for row in lo["rows"])
    hi = query_dataset_rows(
        "collisions", filters={"latitude": {"max": 40.6}}, limit=10
    )
    assert "error" not in hi
    assert all(row["latitude"] <= 40.6 for row in hi["rows"])


def test_unsupported_dict_shape_is_structured_error_not_500():
    out = query_dataset_rows(
        "collisions", filters={"latitude": {"between": [40.7, 40.8]}}
    )
    assert "error" in out
    assert "retry_hint" in out
    assert "range" in out["error"]  # the error teaches the supported shapes


def test_nested_nonscalar_bounds_are_rejected():
    out = query_dataset_rows(
        "collisions", filters={"latitude": {"min": [40.7]}}
    )
    assert "error" in out


def test_unknown_filter_keys_keep_their_explicit_note():
    out = query_dataset_rows("collisions", filters={"radius_m": 500}, limit=5)
    assert "error" not in out
    assert out["ignored_filters"] == ["radius_m"]
    assert "not applied" in out["ignored_filters_note"]
