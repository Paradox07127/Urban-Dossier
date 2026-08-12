"""The population denominator must conserve people and partition cleanly.

Conservation is the non-negotiable: an allocation that invents or loses
residents poisons every per-capita rate built on it later. The synthetic
fixtures pin that plus the partition property (a cell belongs to exactly one
tract) and the sub-cell fallback; the real-data layer pins the exact ACS
total, because "close" is not a property a denominator gets to have.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

_spec = importlib.util.spec_from_file_location(
    "build_population_denominator",
    REPO_ROOT / "backend" / "scripts" / "build_population_denominator.py",
)
bpd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spec and bpd)

gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")


def _tracts(rows):
    from shapely.geometry import box

    return gpd.GeoDataFrame(
        {"GEOID": [r[0] for r in rows]},
        geometry=[box(*r[1]) for r in rows],
        crs=4326,
    )


# Two adjacent boxes over Manhattan, each big enough to hold many r9 centres.
WEST = ("36061000100", (-74.00, 40.72, -73.985, 40.735))
EAST = ("36061000200", (-73.985, 40.72, -73.97, 40.735))
TINY = ("36061000300", (-73.9761, 40.7271, -73.9759, 40.7273))  # sub-cell, well inside EAST


def _totals(edges):
    out = {}
    for cell, _, share, _ in edges:
        out[cell] = out.get(cell, 0.0) + share
    return out


def test_population_is_conserved_exactly():
    populations = {WEST[0]: (9000, 100), EAST[0]: (4000, 80)}
    edges, fallbacks = bpd.allocate(populations, _tracts([WEST, EAST]))
    assert sum(v for _, _, v, _ in edges) == pytest.approx(13000, abs=1e-9)
    assert fallbacks == 0


def test_cells_partition_between_adjacent_tracts():
    """Centre containment: no cell is paid by both neighbours."""
    edges, _ = bpd.allocate(
        {WEST[0]: (9000, 100), EAST[0]: (4000, 80)}, _tracts([WEST, EAST])
    )
    tracts_per_cell = {}
    for cell, tract, _, _ in edges:
        tracts_per_cell.setdefault(cell, set()).add(tract)
    assert all(len(t) == 1 for t in tracts_per_cell.values())


def test_equal_split_within_a_tract():
    edges, _ = bpd.allocate({WEST[0]: (9000, 100)}, _tracts([WEST]))
    shares = {round(v, 6) for _, _, v, _ in edges}
    assert len(shares) == 1
    assert len(edges) > 10


def test_a_sub_cell_tract_donates_via_representative_point():
    edges, fallbacks = bpd.allocate({TINY[0]: (500, 50)}, _tracts([TINY]))
    assert fallbacks == 1
    assert sum(v for _, _, v, _ in edges) == pytest.approx(500)
    assert len(edges) == 1


def test_overlapping_fallback_keeps_both_tracts_in_provenance():
    """The review finding: a fallback donation landing on an occupied cell
    used to erase the smaller tract's identity. Edges keep both."""
    import h3

    # TINY sits inside EAST's box, so its fallback cell is one of EAST's cells.
    populations = {EAST[0]: (4000, 80), TINY[0]: (500, 50)}
    edges, fallbacks = bpd.allocate(populations, _tracts([EAST, TINY]))
    assert fallbacks == 1
    tracts_seen = {tract for _, tract, _, _ in edges}
    assert tracts_seen == {EAST[0], TINY[0]}
    # And the shared cell carries contributions from both.
    tiny_cell = [c for c, t, _, _ in edges if t == TINY[0]][0]
    contributors = {t for c, t, _, _ in edges if c == tiny_cell}
    assert TINY[0] in contributors and EAST[0] in contributors


def test_moe_shares_scale_with_population_shares():
    edges, _ = bpd.allocate({WEST[0]: (9000, 300)}, _tracts([WEST]))
    for _, _, share, moe_share in edges:
        assert moe_share == pytest.approx(share * 300 / 9000)


def test_loader_keeps_moe_and_filters_to_nyc(tmp_path):
    dat = tmp_path / "b01003.dat"
    dat.write_text(
        "GEO_ID|B01003_E001|B01003_M001\n"
        "1400000US36005000100|3200|210\n"      # Bronx: kept
        "1400000US36119000100|9999|1\n"        # Westchester: dropped
        "1400000US06037000100|8888|1\n"        # California: dropped
    )
    out = bpd.load_population(dat)
    assert out == {"36005000100": (3200, 210)}


# --- the real artifact -------------------------------------------------------

ARTIFACT = Path("/mnt/data/Urban-Dossier/data/ready/context/population_r9.parquet")
MANIFEST = ARTIFACT.with_name("population_r9.manifest.json")

requires_artifact = pytest.mark.skipif(
    not ARTIFACT.exists(), reason="population artifact not built"
)


@requires_artifact
def test_real_allocation_conserves_the_acs_total_reread_from_the_artifact():
    """Trust the parquet, not the manifest's intention (review finding: the
    old test compared pre-write numbers; the written file had drifted 0.099
    people through per-cell rounding)."""
    import duckdb

    manifest = json.loads(MANIFEST.read_text())
    total = duckdb.connect().execute(
        f"SELECT sum(population) FROM read_parquet('{ARTIFACT.as_posix()}')"
    ).fetchone()[0]
    assert total == pytest.approx(manifest["acs_population"], rel=1e-9)
    assert manifest["acs_population"] > 8_000_000


@requires_artifact
def test_every_input_tract_survives_into_provenance():
    import duckdb

    manifest = json.loads(MANIFEST.read_text())
    prov = ARTIFACT.with_name("population_r9_provenance.parquet")
    distinct = duckdb.connect().execute(
        f"SELECT count(DISTINCT tract_geoid) FROM read_parquet('{prov.as_posix()}')"
    ).fetchone()[0]
    assert distinct == manifest["tracts"] == 2327


@requires_artifact
def test_manifest_gates_the_release():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["source"]["population_sha256"]
    assert manifest["source"]["geometry_sha256"]
    for name, meta in manifest["artifacts"].items():
        assert meta["sha256"] and meta["bytes"] > 0 and meta["rows"] > 0, name


@requires_artifact
def test_denominator_covers_the_scored_city():
    import duckdb

    con = duckdb.connect()
    overlap, scored = con.execute(
        f"""
        SELECT count(p.h3_r9), count(*)
        FROM read_parquet('/mnt/data/Urban-Dossier/data/ready/safety/collisions_scores_h3.parquet') s
        LEFT JOIN read_parquet('{ARTIFACT.as_posix()}') p USING (h3_r9)
        """
    ).fetchone()
    assert overlap / scored > 0.9, f"only {overlap}/{scored} scored cells have population"


@requires_artifact
def test_no_cell_holds_an_implausible_share_of_the_city():
    import duckdb

    top = duckdb.connect().execute(
        f"SELECT max(population) FROM read_parquet('{ARTIFACT.as_posix()}')"
    ).fetchone()[0]
    assert top < 50_000, top
