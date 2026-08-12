"""The baked per-building scores must equal what the API reports for that spot.

This is the whole premise of colouring buildings from a tileset. The map is
allowed to be fast, but it is not allowed to be a second opinion: if a building
is painted 62 the detail panel for that building has to say 62 too. The
previous implementation failed exactly here -- it took the containing hex's
score and added a coordinate hash worth +/-4, so the colour and the number
disagreed by construction and nothing caught it.

These tests compare the batch pass in ``backend/scripts/score_buildings.py``
against ``compute_secondary_scores`` driven by the provider's own lookup, for a
sample of real buildings. They skip rather than fail when the artefacts are
absent, so a checkout that has not run the pipeline still has a green suite.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urban_dossier_backend.metrics import METHODOLOGY_VERSION

BUILDINGS_DIR = Path("/mnt/data/urban-dossier-state/maps/buildings")
SCORES = BUILDINGS_DIR / "building_scores.parquet"
MANIFEST = BUILDINGS_DIR / "building_scores.manifest.json"
TILE_MANIFEST = BUILDINGS_DIR / "building_tiles.manifest.json"


def _baked_version() -> str | None:
    try:
        return json.loads(MANIFEST.read_text()).get("methodology_version")
    except (OSError, json.JSONDecodeError):
        return None


# Two distinct skip conditions, kept apart because they mean different things.
# Absent artefacts are a checkout that never ran the pipeline. A version
# mismatch is a bake from an older methodology: comparing it against current
# code would fail on every weight change by design, which punishes the change
# rather than the staleness. The remedy for the second skip is a re-bake --
# and it is a *loud* skip, not a pass: the tiles on screen carry the old
# weights until someone runs score_buildings.py again.
pytestmark = [
    pytest.mark.skipif(
        not SCORES.exists(),
        reason="building_scores.parquet not built; run backend/scripts/score_buildings.py",
    ),
    pytest.mark.skipif(
        SCORES.exists() and _baked_version() != METHODOLOGY_VERSION,
        reason=(
            f"baked scores are methodology {_baked_version() or 'pre-versioning'}, "
            f"code is {METHODOLOGY_VERSION}; re-run backend/scripts/score_buildings.py "
            "and rebuild the building tiles"
        ),
    ),
]


@pytest.fixture(scope="module")
def sample_rows():
    import duckdb

    con = duckdb.connect()
    # Deterministic sample: ordering by a hash of the id keeps the same
    # buildings across runs without depending on file order.
    return con.execute(
        f"""
        SELECT bldg_id, lat, lon, h3_r9, safety, transit, amenities, building, overall
        FROM read_parquet('{SCORES.as_posix()}')
        WHERE overall IS NOT NULL
        ORDER BY hash(bldg_id)
        LIMIT 40
        """
    ).fetchall()


def test_sample_is_not_empty(sample_rows):
    assert len(sample_rows) == 40, "expected a full sample of scored buildings"


def test_scores_are_in_range(sample_rows):
    for row in sample_rows:
        for value in row[4:]:
            if value is not None:
                assert 0 <= value <= 100, f"score out of range: {row}"


def test_score_artifact_hash_and_radius_contract_are_current():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["scoring_contract"] == "point-radius-haversine-v1"
    assert manifest["radius_m"] == 500
    assert manifest["building_radius_m"] == 250
    assert manifest["artifact_sha256"] == hashlib.sha256(SCORES.read_bytes()).hexdigest()
    if TILE_MANIFEST.exists():
        tile_manifest = json.loads(TILE_MANIFEST.read_text())
        assert tile_manifest["methodology_version"] == METHODOLOGY_VERSION
        assert tile_manifest["scoring_contract"] == manifest["scoring_contract"]
        assert tile_manifest["source_scores_sha256"] == manifest["artifact_sha256"]


def test_baked_score_equals_the_provider_score(sample_rows):
    """Replay the provider's own lookup and demand an exact match."""
    from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider
    from urban_dossier_backend.secondary_scoring import compute_secondary_scores

    provider = DirectQueryDataProvider()
    con = provider._connect()

    # Distinguish "the provider disagrees" from "the provider has no data
    # configured". Without URBAN_DOSSIER_READY_ROOT every lookup returns None
    # and every comparison would fail, which reads as a scoring bug and is not
    # one. A misconfigured environment is a skip; a real disagreement is not.
    probe_lat, probe_lon = sample_rows[0][1], sample_rows[0][2]
    probe = provider._collect_prepared_scores(con, probe_lat, probe_lon, 500, None)
    if not probe:
        pytest.skip(
            "DirectQueryDataProvider returned no prepared scores -- set "
            "URBAN_DOSSIER_READY_ROOT to the ready data root to run this check"
        )

    mismatches = []
    for bldg_id, lat, lon, _cell, safety, transit, amenities, building, overall in sample_rows:
        prepared = provider._collect_prepared_scores(con, lat, lon, 500, None)
        expected = compute_secondary_scores(
            current_state={}, baselines={}, prepared_scores=prepared
        )
        # ZIP-grained sub-signals are resolved from the address in the live path
        # and left out of the batch pass, so compare only the categories the
        # batch pass claims to own.
        for name, baked in (
            ("safety", safety),
            ("transit", transit),
            ("amenities", amenities),
            ("building", building),
        ):
            live = expected.get(name)
            if live is None and baked is None:
                continue
            if live != baked:
                mismatches.append((bldg_id, name, baked, live))

    assert not mismatches, (
        "baked scores disagree with the provider for "
        f"{len(mismatches)} (building, category) pairs; first 10: {mismatches[:10]}"
    )


def test_same_cell_can_preserve_real_radius_boundary_variation(sample_rows):
    """Exact metric buffers may differ inside one H3 cell.

    This is not the retired coordinate jitter: the live provider filters
    candidate-cell centroids by distance from the actual building coordinate.
    Flattening the result to one value per containing cell makes edge buildings
    disagree with their detail response.
    """
    import duckdb

    con = duckdb.connect()
    row = con.execute(
        f"""
        SELECT h3_r9, count(*) AS n, count(DISTINCT overall) AS distinct_scores
        FROM read_parquet('{SCORES.as_posix()}')
        WHERE overall IS NOT NULL
        GROUP BY h3_r9
        HAVING count(*) > 50 AND count(DISTINCT overall) > 1
        ORDER BY n DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None, (
        "all buildings in each r9 cell were flattened to one score; "
        "score_buildings.py may have regressed to the old grid_disk bake"
    )
    _cell, n, distinct_scores = row
    assert n > 50
    assert distinct_scores > 1
