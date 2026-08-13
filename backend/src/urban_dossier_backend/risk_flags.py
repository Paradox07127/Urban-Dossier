"""The building Risk Flag -- P0-02 resolved, per product decision 2026-08-12.

Building never earned a place in `overall`: its two metrics measure "how
distressed is the housing stock here", which is a warning, not a wellbeing
dimension, and its signals entangle with the sanitation construct (measured
rho 0.93 against housing violations). Rather than a fourth weighted category,
it is now an explicit flag: a small, absolute, threshold-based statement that
seriously distressed buildings are (or are not) near this location.

Absolute rather than percentile, deliberately. The 0-100 building score
still exists for relative reading, but a *flag* must be able to say "there
is nothing here" -- and a citywide percentile cannot, because somebody is
always in the 30th percentile of nothing much. The thresholds are anchored
to the city's own severity language:

* AEP membership is HPD's designation of the most distressed multiple
  dwellings in New York -- roughly 250 buildings a year citywide. One within
  the radius is exceptional by construction.
* Class C violations are "immediately hazardous" in HPD's own taxonomy;
  class B are "hazardous".

Levels, worst first:

    serious   an AEP building nearby AND open immediately-hazardous
              violations -- the city's worst-buildings list corroborated by
              live class C conditions
    elevated  an AEP building nearby, or a cluster (>= 5) of open class C
    watch     any open class C, or a heavy pile (>= 10) of open class B
    none      building data present, none of the above
    unknown   no building signals in the payload at all -- absence of data,
              never presented as absence of risk

The thresholds are declared choices, recorded here and served by the
methodology endpoint; they are inputs for the sensitivity analysis to
perturb, not truths. Changing any of them is a methodology-version event.
"""
from __future__ import annotations

from typing import Any

# Served via /api/metrics so the methodology page renders the real contract.
BUILDING_RISK_FLAG_SPEC = {
    "id": "building_risk",
    "label": "Building risk",
    "role": "risk_flag",
    "basis": "absolute thresholds on HPD severity classes, not percentiles",
    "inputs": ["aep_count_250m", "open_class_c_250m", "open_class_b_250m"],
    "levels": [
        {
            "level": "serious",
            "rule": "aep_count_250m >= 1 AND open_class_c_250m >= 1",
            "meaning": "a building on the city's Alternative Enforcement "
                       "Program list is nearby and open immediately-hazardous "
                       "violations are present",
        },
        {
            "level": "elevated",
            "rule": "aep_count_250m >= 1 OR open_class_c_250m >= 5",
            "meaning": "either an AEP building nearby, or a cluster of open "
                       "immediately-hazardous violations",
        },
        {
            "level": "watch",
            "rule": "open_class_c_250m >= 1 OR open_class_b_250m >= 10",
            "meaning": "at least one open immediately-hazardous violation, or "
                       "a heavy accumulation of hazardous ones",
        },
        {"level": "none", "rule": "building data present, none of the above",
         "meaning": "no distress signals within the radius"},
        {"level": "unknown", "rule": "no building signals in the payload",
         "meaning": "absence of data, not absence of risk"},
    ],
}


def building_risk_flag(current_state: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the flag from the analyse-point building state. Pure."""
    building = (current_state or {}).get("building") or {}
    aep = building.get("aep_count_250m")
    class_c = building.get("open_class_c_250m")
    class_b = building.get("open_class_b_250m")

    if aep is None and class_c is None and class_b is None:
        return {
            "level": "unknown",
            "reasons": ["no building signals in the payload"],
            "counts": {},
        }

    aep_n = int(aep or 0)
    c_n = int(class_c or 0)
    b_n = int(class_b or 0)
    counts = {"aep_250m": aep_n, "open_class_c_250m": c_n, "open_class_b_250m": b_n}

    reasons: list[str] = []
    if aep_n >= 1 and c_n >= 1:
        level = "serious"
        reasons.append(
            f"{aep_n} Alternative Enforcement Program building(s) within 250 m"
        )
        reasons.append(f"{c_n} open immediately-hazardous (class C) violation(s)")
    elif aep_n >= 1 or c_n >= 5:
        level = "elevated"
        if aep_n >= 1:
            reasons.append(
                f"{aep_n} Alternative Enforcement Program building(s) within 250 m"
            )
        if c_n >= 5:
            reasons.append(f"{c_n} open immediately-hazardous (class C) violation(s)")
    elif c_n >= 1 or b_n >= 10:
        level = "watch"
        if c_n >= 1:
            reasons.append(f"{c_n} open immediately-hazardous (class C) violation(s)")
        if b_n >= 10:
            reasons.append(f"{b_n} open hazardous (class B) violation(s)")
    else:
        level = "none"
        reasons.append("no distress signals within the radius")

    return {"level": level, "reasons": reasons, "counts": counts}
