"""Serve the sensitivity analysis's per-cell intervals at request time.

Item 1.4's remaining acceptance criterion: the API returns score intervals,
not just points. The intervals are not computed here -- they come from the
offline 1,000-draw Monte Carlo in `backend/scripts/run_sensitivity_analysis.py`
(seeded, regenerable), which writes `analysis/sensitivity_cells.parquet` under
the ready root. This module is only the lookup.

Two intervals are served, because they answer different questions:

``score_range``            holds normalization at the production method;
                           spans weights, the flagged-metric toggle and the
                           missing-data rule. "Given how we score, how firm
                           is this number?" -- the one a UI should lead with.
``score_range_all_methods`` additionally spans normalization substitution.
                           Wider, and honest about methodological freedom.

The table is loaded once per process and served from memory (~7k rows). A
missing or unreadable artifact yields None rather than an error: uncertainty
disclosure must never take down the analysis it annotates, and an absent
interval is itself information the caller can surface as "not quantified".
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

from .config import READY_DATA_DIR
from .metrics import METRICS, METHODOLOGY_VERSION

logger = logging.getLogger(__name__)

_CELLS_PATH = READY_DATA_DIR / "analysis" / "sensitivity_cells.parquet"
_MANIFEST_PATH = READY_DATA_DIR / "analysis" / "sensitivity_cells.manifest.json"
_INPUT_PATHS = {
    metric.id: READY_DATA_DIR / metric.score_table
    for metric in METRICS
    if metric.spatial_grain.value == "h3_r9"
}
_EXPECTED_SCHEMA_VERSION = "1.0"
_EXPECTED_COLUMNS = [
    "h3_r9", "nominal", "median", "lo95", "hi95",
    "lo95_prodnorm", "hi95_prodnorm",
    "rank_nominal", "rank_median", "rank_p5", "rank_p95",
]
PUBLIC_TIER_BANDS = (
    {"id": "very_low", "label": "Very low", "score_min": 0, "score_max": 20},
    {"id": "low", "label": "Low", "score_min": 20, "score_max": 40},
    {"id": "middle", "label": "Middle", "score_min": 40, "score_max": 60},
    {"id": "high", "label": "High", "score_min": 60, "score_max": 80},
    {"id": "very_high", "label": "Very high", "score_min": 80, "score_max": 100},
)
_lock = threading.Lock()
_cache: dict[str, dict] | None = None
_city_distribution: dict | None = None
_publication: dict | None = None
_cache_fingerprint: tuple | None = None
_cache_missing = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> tuple[bool, int, int]:
    try:
        stat = path.stat()
        return True, stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return False, 0, 0


def _current_fingerprint() -> tuple:
    return (
        _file_fingerprint(_CELLS_PATH),
        _file_fingerprint(_MANIFEST_PATH),
        tuple(
            (metric_id, _file_fingerprint(path))
            for metric_id, path in sorted(_INPUT_PATHS.items())
        ),
    )


def _tier_index(score: float) -> int:
    bounded = max(0.0, min(100.0, score))
    return min(int(bounded // 20), len(PUBLIC_TIER_BANDS) - 1)


def public_tier(score_range: list[float | None]) -> dict | None:
    """A coarse public label driven by the production-method 95% interval."""
    if len(score_range) != 2 or any(value is None for value in score_range):
        return None
    low, high = float(score_range[0]), float(score_range[1])
    if low > high:
        return None
    lower = PUBLIC_TIER_BANDS[_tier_index(low)]
    upper = PUBLIC_TIER_BANDS[_tier_index(high)]
    label = (
        lower["label"]
        if lower["id"] == upper["id"]
        else f"{lower['label']}–{upper['label']}"
    )
    return {
        "schema_version": "1.0",
        "scale": "fixed_20_point_score_bands",
        "basis": "production_normalization_95pct_interval",
        "label": label,
        "spans_multiple_tiers": lower["id"] != upper["id"],
        "lower": dict(lower),
        "upper": dict(upper),
        "score_range": [round(low, 1), round(high, 1)],
    }


def _load() -> dict[str, dict] | None:
    global _cache, _city_distribution, _publication, _cache_fingerprint, _cache_missing
    fingerprint = _current_fingerprint()
    if _cache_fingerprint == fingerprint and (_cache is not None or _cache_missing):
        return _cache
    with _lock:
        fingerprint = _current_fingerprint()
        if _cache_fingerprint == fingerprint and (_cache is not None or _cache_missing):
            return _cache
        _cache = None
        _city_distribution = None
        _publication = None
        _cache_missing = False
        _cache_fingerprint = fingerprint
        if not _CELLS_PATH.exists() or not _MANIFEST_PATH.exists():
            logger.info("sensitivity artifact or manifest absent; intervals unavailable")
            _cache_missing = True
            return None
        try:
            import duckdb

            manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
            artifact = manifest.get("artifact") or {}
            manifest_inputs = manifest.get("input_score_tables") or {}
            expected_inputs = {
                metric_id: path
                for metric_id, path in _INPUT_PATHS.items()
                if path.exists()
            }
            inputs_valid = set(manifest_inputs) == set(expected_inputs)
            if inputs_valid:
                for metric_id, path in expected_inputs.items():
                    stamp = manifest_inputs.get(metric_id) or {}
                    relative_path = path.relative_to(READY_DATA_DIR).as_posix()
                    if not (
                        stamp.get("path") == relative_path
                        and stamp.get("size_bytes") == path.stat().st_size
                        and stamp.get("sha256") == _sha256(path)
                    ):
                        inputs_valid = False
                        break
            valid = (
                manifest.get("schema_version") == _EXPECTED_SCHEMA_VERSION
                and manifest.get("methodology_version") == METHODOLOGY_VERSION
                and artifact.get("filename") == _CELLS_PATH.name
                and artifact.get("columns") == _EXPECTED_COLUMNS
                and isinstance(artifact.get("row_count"), int)
                and artifact.get("row_count", 0) > 0
                and artifact.get("sha256") == _sha256(_CELLS_PATH)
                and artifact.get("size_bytes") == _CELLS_PATH.stat().st_size
                and isinstance(manifest.get("draws"), int)
                and manifest.get("draws", 0) > 0
                and inputs_valid
            )
            if not valid:
                raise ValueError("sensitivity publication manifest mismatch")
            cursor = duckdb.connect().execute(
                f"SELECT * FROM read_parquet('{_CELLS_PATH.as_posix()}')"
            )
            columns = [description[0] for description in cursor.description]
            if columns != _EXPECTED_COLUMNS:
                raise ValueError("sensitivity artifact column mismatch")
            rows = cursor.fetchall()
            if len(rows) != artifact["row_count"]:
                raise ValueError("sensitivity artifact row count mismatch")
            _cache = {row[0]: dict(zip(columns[1:], row[1:])) for row in rows}
            _publication = {
                "artifact_version": artifact["sha256"],
                "artifact_generated": manifest.get("generated"),
                "draws": manifest["draws"],
            }
            nominal_scores = [
                float(entry["nominal"])
                for entry in _cache.values()
                if entry["nominal"] is not None
            ]
            bin_width = 5
            counts = [0] * (100 // bin_width)
            for score in nominal_scores:
                index = min(max(int(score // bin_width), 0), len(counts) - 1)
                counts[index] += 1
            _city_distribution = {
                "grain": "h3_r9_analysis_cells",
                "score_field": "nominal",
                "population_n": len(nominal_scores),
                "bin_width": bin_width,
                "bins": [
                    {
                        "bin_start": index * bin_width,
                        "bin_end": (index + 1) * bin_width,
                        "count": count,
                    }
                    for index, count in enumerate(counts)
                ],
                "method": "midrank_ecdf",
            }
            logger.info(
                "loaded %d cells of score intervals from artifact %.12s",
                len(_cache), artifact["sha256"],
            )
        except Exception as exc:  # noqa: BLE001 - disclosure must not break analysis
            logger.warning("failed to load sensitivity cells: %s", exc)
            _cache_missing = True
            return None
    return _cache


def score_uncertainty(latitude: float, longitude: float) -> dict | None:
    """The uncertainty summary for the cell containing a point, or None.

    Cell-grain on purpose: the offline analysis perturbs scoring assumptions
    at cell level, and pretending radius-level precision here would misstate
    what was actually measured.
    """
    cells = _load()
    if cells is None:
        return None
    import h3

    entry = cells.get(h3.latlng_to_cell(latitude, longitude, 9))
    if entry is None:
        return None

    def _round(value: float | None) -> float | None:
        return None if value is None else round(float(value), 1)

    nominal_score = _round(entry["nominal"])
    nominal_values = [
        float(item["nominal"])
        for item in cells.values()
        if item["nominal"] is not None
    ]
    nominal_percentile = None
    if nominal_score is not None and nominal_values:
        below = sum(value < float(entry["nominal"]) for value in nominal_values)
        equal = sum(value == float(entry["nominal"]) for value in nominal_values)
        nominal_percentile = round((below + 0.5 * equal) / len(nominal_values), 4)
    distribution = None
    if _city_distribution is not None:
        distribution = {
            **_city_distribution,
            "marker_score": nominal_score,
            "marker_percentile": nominal_percentile,
        }

    score_range = [_round(entry["lo95_prodnorm"]), _round(entry["hi95_prodnorm"])]
    publication = _publication or {}
    return {
        "grain": "h3_r9_cell",
        "methodology_version": METHODOLOGY_VERSION,
        "artifact_version": publication.get("artifact_version"),
        "artifact_generated": publication.get("artifact_generated"),
        "draws": publication.get("draws"),
        "score_median": _round(entry["median"]),
        "nominal_score": nominal_score,
        "nominal_percentile": nominal_percentile,
        "distribution": distribution,
        "score_range": score_range,
        "public_tier": public_tier(score_range),
        "score_range_all_methods": [_round(entry["lo95"]), _round(entry["hi95"])],
        "rank_range_share": None if entry["rank_p5"] is None or entry["rank_p95"] is None
        else [
            round(float(entry["rank_p5"]) / max(len(cells), 1), 4),
            round(float(entry["rank_p95"]) / max(len(cells), 1), 4),
        ],
        "note": (
            "score_range holds the production normalization and varies "
            "weights, metric inclusion and the missing-data rule; "
            "score_range_all_methods additionally varies the normalization "
            "method. public_tier maps the production-method 95% interval to "
            "fixed 20-point bands, so the headline does not imply point "
            "precision. Seeded Monte Carlo draws; count and artifact version "
            "come from the validated publication manifest."
        ),
    }


def reset_cache() -> None:
    """Test hook: drop the memoized table so a new path can be exercised."""
    global _cache, _city_distribution, _publication, _cache_fingerprint, _cache_missing
    with _lock:
        _cache = None
        _city_distribution = None
        _publication = None
        _cache_fingerprint = None
        _cache_missing = False
