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
TINY = ("36061000300", (-73.9701, 40.7201, -73.9699, 40.7203))  # sub-cell


def test_population_is_conserved_exactly():
    populations = {WEST[0]: (9000, 100), EAST[0]: (4000, 80)}
    cells, _, fallbacks = bpd.allocate(populations, _tracts([WEST, EAST]))
    assert sum(cells.values()) == pytest.approx(13000, abs=1e-9)
    assert fallbacks == 0


def test_cells_partition_between_adjacent_tracts():
    """Centre containment: no cell is paid by both neighbours."""
    populations = {WEST[0]: (9000, 100), EAST[0]: (0, 0)}
    cells_west, _, _ = bpd.allocate({WEST[0]: (9000, 100)}, _tracts([WEST]))
    cells_both, tract_of, _ = bpd.allocate(populations, _tracts([WEST, EAST]))
    # Every populated cell's money comes from the west tract only.
    for cell, pop in cells_both.items():
        if pop > 0:
            assert cell in cells_west
            assert tract_of[cell] == WEST[0]


def test_equal_split_within_a_tract():
    populations = {WEST[0]: (9000, 100)}
    cells, _, _ = bpd.allocate(populations, _tracts([WEST]))
    shares = set(round(v, 6) for v in cells.values())
    assert len(shares) == 1  # uniform by construction
    assert len(cells) > 10   # the box really does span many cells


def test_a_sub_cell_tract_donates_via_representative_point():
    populations = {TINY[0]: (500, 50)}
    cells, tract_of, fallbacks = bpd.allocate(populations, _tracts([TINY]))
    assert fallbacks == 1
    assert sum(cells.values()) == pytest.approx(500)
    assert len(cells) == 1
    assert list(tract_of.values()) == [TINY[0]]


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
def test_real_allocation_conserves_the_acs_total_to_the_person():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["allocated_population"] == pytest.approx(
        manifest["acs_population"], abs=0.01
    )
    assert manifest["acs_population"] > 8_000_000  # it really is New York


@requires_artifact
def test_denominator_covers_the_scored_city():
    """Cells the scoring frame knows about should overwhelmingly have people."""
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
