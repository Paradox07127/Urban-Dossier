"""The low-zoom overview must be the same methodology as everything else.

Born from a review finding: `build_overview_tiles.py` carried its own
hand-written weight table -- the third independent copy in the repo, matching
no era of the real config -- and kept reading the retired collision copy and
the count-based rodent table. The served artifacts dated from August 2 while
buildings and the detail panel served v3.9.0, and with no version stamp
nothing could tell. One screen, three methodologies, all of them returning
200.

Three layers of defence, cheapest first: the derivation is asserted against
the registry (no hand-written table to drift), the artifacts carry a version
stamp that must equal the code's (a mismatch FAILS -- regeneration takes
about a minute, so unlike the building bake there is no excuse to skip), and
a numeric reconciliation recomputes cells from the ready tables through the
same derivation and demands agreement.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from urban_dossier_backend.metrics import (  # noqa: E402
    CATEGORIES_BY_ID,
    METHODOLOGY_VERSION,
    METRICS_BY_ID,
    metrics_for_category,
)

_spec = importlib.util.spec_from_file_location(
    "build_overview_tiles", REPO_ROOT / "backend" / "scripts" / "build_overview_tiles.py"
)
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spec and bot)

OVERVIEW_DIR = Path("/mnt/data/Urban-Dossier/data/cache/overview")
READY = Path("/mnt/data/Urban-Dossier/data/ready")

requires_artifacts = pytest.mark.skipif(
    not (OVERVIEW_DIR / "overview_overall_h3_r8.parquet").exists(),
    reason="overview artifacts not built",
)


# --- derivation: no hand-written weights left --------------------------------


def test_category_sources_come_from_the_registry():
    """Each category's tile weights are its H3 metrics, renormalised."""
    for category_id, pairs in bot.CATEGORY_SOURCES.items():
        h3_metrics = [
            m for m in metrics_for_category(category_id)
            if m.spatial_grain.value == "h3_r9"
        ]
        expected_tables = {m.score_table for m in h3_metrics}
        assert {rel for _, rel in pairs} == expected_tables
        weight_sum = sum(m.weight_in_category for m in h3_metrics)
        by_table = {m.score_table: m.weight_in_category / weight_sum for m in h3_metrics}
        for weight, rel in pairs:
            assert weight == pytest.approx(by_table[rel]), rel
        assert sum(w for w, _ in pairs) == pytest.approx(1.0)


def test_retired_tables_are_not_read():
    """The two tables v3.8/3.9 retired must never reappear here."""
    all_tables = [rel for pairs in bot.CATEGORY_SOURCES.values() for _, rel in pairs]
    assert "transit/collision_transport_scores_h3.parquet" not in all_tables
    assert "safety/rodent_scores_h3.parquet" not in all_tables
    assert "safety/rodent_rate_scores_h3.parquet" in all_tables


def test_overall_weights_are_the_registry_categories_renormalised():
    expected_total = sum(
        c.weight_in_overall
        for c in CATEGORIES_BY_ID.values()
        if c.map_driving and c.weight_in_overall > 0
    )
    for category_id, weight in bot.OVERALL_WEIGHTS.items():
        assert weight == pytest.approx(
            CATEGORIES_BY_ID[category_id].weight_in_overall / expected_total
        )
    assert sum(bot.OVERALL_WEIGHTS.values()) == pytest.approx(1.0)


# --- version stamp -----------------------------------------------------------


@requires_artifacts
def test_artifacts_carry_the_current_methodology_version():
    """A mismatch here FAILS, deliberately.

    Regenerating takes about a minute (build_overview_tiles.py then
    build_overview_nta.py), so a stale overview is a fixable offence, not a
    skippable condition. This is the test that would have caught the August
    artifacts serving three-versions-old weights.
    """
    manifest = OVERVIEW_DIR / "overview.manifest.json"
    assert manifest.exists(), (
        "overview artifacts have no version manifest -- they predate "
        "versioning; re-run build_overview_tiles.py"
    )
    stamped = json.loads(manifest.read_text())["methodology_version"]
    assert stamped == METHODOLOGY_VERSION, (
        f"overview artifacts are methodology {stamped}, code is "
        f"{METHODOLOGY_VERSION}; re-run build_overview_tiles.py and "
        "build_overview_nta.py"
    )


@requires_artifacts
def test_nta_artifacts_are_bound_to_current_h3_sources():
    manifest = json.loads((OVERVIEW_DIR / "overview.manifest.json").read_text())
    nta = manifest.get("nta")
    assert nta, "NTA artifacts are unstamped; re-run build_overview_nta.py"
    assert nta["methodology_version"] == METHODOLOGY_VERSION

    for tag in ("overall", "safety", "transit", "amenities"):
        h3_path = OVERVIEW_DIR / f"overview_{tag}_h3_r8.parquet"
        json_path = OVERVIEW_DIR / f"overview_{tag}_nta.json"
        zones = json.loads(json_path.read_text())
        assert nta["zones"][tag] == len(zones) > 0
        assert nta["source_h3_sha256"][tag] == hashlib.sha256(
            h3_path.read_bytes()
        ).hexdigest()
        assert nta["json_sha256"][tag] == hashlib.sha256(
            json_path.read_bytes()
        ).hexdigest()


@requires_artifacts
def test_h3_and_nta_artifacts_disclose_count_and_weighted_coverage():
    import pandas as pd

    for tag in ("overall", "safety", "transit", "amenities"):
        prefix = "overall" if tag == "overall" else tag
        required = {
            f"{prefix}_coverage_n",
            f"{prefix}_coverage_total",
            f"{prefix}_coverage_fraction",
            f"{prefix}_coverage_ratio",
        }
        for suffix in ("h3_r8", "nta"):
            path = OVERVIEW_DIR / f"overview_{tag}_{suffix}.parquet"
            frame = pd.read_parquet(path)
            assert required <= set(frame.columns), path
            n = frame[f"{prefix}_coverage_n"]
            total = frame[f"{prefix}_coverage_total"]
            fraction = frame[f"{prefix}_coverage_fraction"]
            ratio = frame[f"{prefix}_coverage_ratio"]
            assert (n >= 0).all()
            assert (n <= total).all()
            assert ((fraction >= 0) & (fraction <= 1)).all()
            assert ((ratio >= 0) & (ratio <= 1)).all()
            assert ((n / total - fraction).abs() <= 0.0001).all()

        # At least one cell must actually disclose incomplete evidence; a
        # constant 1.0 field would satisfy the schema while hiding the issue.
        h3_frame = pd.read_parquet(OVERVIEW_DIR / f"overview_{tag}_h3_r8.parquet")
        assert (h3_frame[f"{prefix}_coverage_ratio"] < 1).any()


def test_node_nta_gate_tracks_the_backend_methodology_version():
    server_source = (REPO_ROOT / "server.js").read_text()
    match = re.search(
        r"const EXPECTED_METHODOLOGY_VERSION = '([^']+)';",
        server_source,
    )
    assert match, "server.js has no explicit NTA methodology gate"
    assert match.group(1) == METHODOLOGY_VERSION


# --- numeric reconciliation --------------------------------------------------


@requires_artifacts
def test_artifact_scores_reconcile_with_ready_tables():
    """Recompute a sample of r8 cells from source and demand agreement.

    Same derivation, same inputs, independently executed: r9 scores averaged
    to the parent r8 cell per metric, then weighted by the registry-derived
    weights over whatever had data. Tolerance is +/-1 for the artifact's
    integer rounding.
    """
    import duckdb
    import h3

    con = duckdb.connect()
    artifact = {
        row[0]: row[1]
        for row in con.execute(
            f"""SELECT h3, safety_score
                FROM read_parquet('{(OVERVIEW_DIR / "overview_safety_h3_r8.parquet").as_posix()}')
                WHERE safety_score IS NOT NULL
                ORDER BY h3 LIMIT 40"""
        ).fetchall()
    }
    assert artifact, "no safety cells in the artifact"

    pairs = bot.CATEGORY_SOURCES["safety"]
    per_metric_r8: list[tuple[float, dict[str, float]]] = []
    for weight, rel in pairs:
        rows = con.execute(
            f"SELECT h3_r9, score FROM read_parquet('{(READY / rel).as_posix()}')"
        ).fetchall()
        acc: dict[str, list[float]] = {}
        for cell_r9, score in rows:
            parent = h3.cell_to_parent(cell_r9, 8)
            acc.setdefault(parent, []).append(float(score))
        per_metric_r8.append((weight, {k: sum(v) / len(v) for k, v in acc.items()}))

    checked = 0
    for cell, artifact_score in artifact.items():
        num = den = 0.0
        for weight, table in per_metric_r8:
            if cell in table:
                num += weight * table[cell]
                den += weight
        if den == 0:
            continue
        expected = num / den
        assert abs(expected - float(artifact_score)) <= 1.0, (
            f"cell {cell}: artifact {artifact_score}, recomputed {expected:.2f}"
        )
        checked += 1
    assert checked >= 30


# --- the payload discloses the version --------------------------------------


@requires_artifacts
def test_overview_payload_reports_the_artifact_version():
    from urban_dossier_backend.providers.direct_provider import DirectQueryDataProvider

    provider = DirectQueryDataProvider()
    if provider.overview_dir is None or not provider.overview_dir.exists():
        pytest.skip("provider overview dir not configured in this environment")
    payload = provider.get_overview_layer("overall", None, None, None)
    assert payload["overview_methodology_version"] == METHODOLOGY_VERSION
