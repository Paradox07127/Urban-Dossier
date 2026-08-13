"""The building Risk Flag: absolute levels, honest unknowns -- P0-02 resolved.

Every boundary in the spec gets a case, because the thresholds ARE the
feature: a flag whose levels drift from its published rules is worse than no
flag. The non-negotiable pair: `unknown` when data is absent (absence of
data must never read as absence of risk), and `none` requiring data to be
present (a flag must be able to say "nothing here" only when it looked).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.metrics import METHODOLOGY_VERSION, registry_to_dict
from urban_dossier_backend.risk_flags import BUILDING_RISK_FLAG_SPEC, building_risk_flag


def flag(**building):
    return building_risk_flag({"building": building})


# --- level boundaries, exactly as published ---------------------------------


def test_serious_needs_aep_corroborated_by_class_c():
    assert flag(aep_count_250m=1, open_class_c_250m=1)["level"] == "serious"
    assert flag(aep_count_250m=3, open_class_c_250m=9)["level"] == "serious"


def test_aep_alone_is_elevated_not_serious():
    out = flag(aep_count_250m=1, open_class_c_250m=0)
    assert out["level"] == "elevated"
    assert any("Alternative Enforcement" in reason for reason in out["reasons"])


def test_a_class_c_cluster_is_elevated_without_aep():
    assert flag(aep_count_250m=0, open_class_c_250m=5)["level"] == "elevated"
    assert flag(aep_count_250m=0, open_class_c_250m=4)["level"] == "watch"


def test_any_class_c_is_at_least_watch():
    assert flag(aep_count_250m=0, open_class_c_250m=1, open_class_b_250m=0)["level"] == "watch"


def test_heavy_class_b_pile_is_watch():
    assert flag(open_class_b_250m=10, open_class_c_250m=0, aep_count_250m=0)["level"] == "watch"
    assert flag(open_class_b_250m=9, open_class_c_250m=0, aep_count_250m=0)["level"] == "none"


def test_clean_data_says_none_with_its_counts():
    out = flag(aep_count_250m=0, open_class_c_250m=0, open_class_b_250m=2)
    assert out["level"] == "none"
    assert out["counts"] == {"aep_250m": 0, "open_class_c_250m": 0, "open_class_b_250m": 2}


def test_absent_data_is_unknown_never_none():
    assert building_risk_flag({})["level"] == "unknown"
    assert building_risk_flag({"building": {}})["level"] == "unknown"


def test_partial_data_still_evaluates():
    """One known signal is data; the flag evaluates on what it has."""
    assert flag(open_class_c_250m=2)["level"] == "watch"


def test_reasons_always_present_and_human_readable():
    for kwargs in (dict(aep_count_250m=1, open_class_c_250m=1),
                   dict(open_class_c_250m=6),
                   dict(open_class_b_250m=12),
                   dict(aep_count_250m=0, open_class_c_250m=0)):
        out = flag(**kwargs)
        assert out["reasons"] and all(isinstance(r, str) for r in out["reasons"])


# --- contract surfaces -------------------------------------------------------


def test_spec_levels_cover_every_producible_level():
    spec_levels = {entry["level"] for entry in BUILDING_RISK_FLAG_SPEC["levels"]}
    produced = {
        flag(aep_count_250m=1, open_class_c_250m=1)["level"],
        flag(aep_count_250m=1)["level"],
        flag(open_class_c_250m=1)["level"],
        flag(open_class_c_250m=0, aep_count_250m=0)["level"],
        building_risk_flag({})["level"],
    }
    assert produced <= spec_levels
    assert produced == {"serious", "elevated", "watch", "none", "unknown"}


def test_registry_serves_the_flag_spec():
    payload = registry_to_dict()
    flags = {entry["id"]: entry for entry in payload["risk_flags"]}
    assert flags["building_risk"]["role"] == "risk_flag"
    assert len(flags["building_risk"]["levels"]) == 5
    assert payload["methodology_version"] == METHODOLOGY_VERSION == "3.10.0"


def test_preview_payload_carries_the_flag():
    from urban_dossier_backend.service import preview_point

    ready = Path("/mnt/data/Urban-Dossier/data/ready")
    if not (ready / "building" / "aep_scores_h3.parquet").exists():
        pytest.skip("ready tables not present")
    payload = preview_point(
        latitude=40.7282, longitude=-73.9942, radius_m=500,
        priority_order=["safety", "transit", "amenities"], time_window_days=365,
    )
    out = payload["building_risk_flag"]
    assert out["level"] in {"serious", "elevated", "watch", "none", "unknown"}
    assert out["reasons"]
