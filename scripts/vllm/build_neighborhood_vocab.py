#!/usr/bin/env python3
"""Freeze the NTA 2020 neighborhood names into evals/agent/nyc_neighborhoods.json.

The eval grader needs to answer "is 'Upper West Side' a real NYC neighborhood
that this answer just asserted out of nowhere?". Reading the boundary geojson
at grade time would make grading depend on a gitignored data artifact and
would stop the graders being unit-testable without the dataset, so the names
are frozen into a small tracked file instead.

Rerun after a boundary refresh:
    python3 scripts/vllm/build_neighborhood_vocab.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SRC = REPO_ROOT / "data" / "boundaries" / "nta_2020.geojson"
OUT = REPO_ROOT / "evals" / "agent" / "nyc_neighborhoods.json"

# Compound NTA labels ("Highbridge Park", "Prospect Park-Lefferts Gardens")
# should also match when a model names just one half of them.
MIN_PART_CHARS = 4


def build(src: Path) -> dict[str, object]:
    features = json.loads(src.read_text(encoding="utf-8"))["features"]
    names: set[str] = set()
    boroughs: set[str] = set()
    for feature in features:
        props = feature.get("properties") or {}
        name = (props.get("NTAName") or "").strip()
        if name:
            names.add(name)
        borough = (props.get("BoroName") or "").strip()
        if borough:
            boroughs.add(borough)

    expanded = set(names)
    for name in names:
        for part in name.replace("(", "-").replace(")", "-").split("-"):
            part = part.strip()
            if len(part) >= MIN_PART_CHARS:
                expanded.add(part)

    return {
        "source": "NYC DCP Neighborhood Tabulation Areas 2020 "
                  "(data/boundaries/nta_2020.geojson)",
        "note": "Frozen so the eval grader stays hermetic and unit-testable. "
                "Regenerate with scripts/vllm/build_neighborhood_vocab.py.",
        "nta_names": len(names),
        "boroughs": sorted(boroughs),
        "neighborhoods": sorted(expanded),
    }


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.is_file():
        print(f"boundary file not found: {src}", file=sys.stderr)
        return 1
    payload = build(src)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{payload['nta_names']} NTA names -> "
        f"{len(payload['neighborhoods'])} vocabulary entries -> {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
