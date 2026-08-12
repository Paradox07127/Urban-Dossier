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

import logging
import threading
from pathlib import Path

from .config import READY_DATA_DIR
from .metrics import METHODOLOGY_VERSION

logger = logging.getLogger(__name__)

_CELLS_PATH = READY_DATA_DIR / "analysis" / "sensitivity_cells.parquet"
_lock = threading.Lock()
_cache: dict[str, dict] | None = None
_cache_missing = False


def _load() -> dict[str, dict] | None:
    global _cache, _cache_missing
    if _cache is not None or _cache_missing:
        return _cache
    with _lock:
        if _cache is not None or _cache_missing:
            return _cache
        if not _CELLS_PATH.exists():
            logger.info("sensitivity_cells.parquet absent; intervals unavailable")
            _cache_missing = True
            return None
        try:
            import duckdb

            rows = duckdb.connect().execute(
                f"SELECT * FROM read_parquet('{_CELLS_PATH.as_posix()}')"
            ).fetchall()
            columns = [
                "h3_r9", "nominal", "median", "lo95", "hi95",
                "lo95_prodnorm", "hi95_prodnorm",
                "rank_nominal", "rank_median", "rank_p5", "rank_p95",
            ]
            _cache = {row[0]: dict(zip(columns[1:], row[1:])) for row in rows}
            logger.info("loaded %d cells of score intervals", len(_cache))
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

    return {
        "grain": "h3_r9_cell",
        "methodology_version": METHODOLOGY_VERSION,
        "draws": 1000,
        "score_median": _round(entry["median"]),
        "score_range": [_round(entry["lo95_prodnorm"]), _round(entry["hi95_prodnorm"])],
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
            "method. 95% intervals over 1,000 seeded Monte Carlo draws."
        ),
    }


def reset_cache() -> None:
    """Test hook: drop the memoized table so a new path can be exercised."""
    global _cache, _cache_missing
    with _lock:
        _cache = None
        _cache_missing = False
