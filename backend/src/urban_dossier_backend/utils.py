from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


def load_json(path: Path | None, default: Any) -> Any:
    if path is None or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    # Datetime / date objects (e.g. returned directly by duckdb when reading
    # the ready parquet files) don't need string parsing.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # NaT/NaN leak from pandas when reading parquet with nulls.
    if text.lower() in {"nat", "nan", "none", "null"}:
        return None
    for fmt in (
        "%m/%d/%Y",
        "%m/%d/%Y %I:%M:%S %p",
        "%Y%m%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            try:
                return datetime.strptime(text[:19], fmt).date()
            except ValueError:
                continue
    return None


def is_within_days(value: Any, days: int) -> bool:
    parsed = parse_date(value)
    if not parsed:
        return False
    age_days = (date.today() - parsed).days
    return 0 <= age_days <= days


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


_NYC_COS_LAT = math.cos(math.radians(40.75))
_DEG_TO_M = 111_320.0


def fast_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth distance in meters. ~5x faster than haversine, <0.15% error for NYC-scale."""
    dlat = (lat2 - lat1) * _DEG_TO_M
    dlon = (lon2 - lon1) * _DEG_TO_M * _NYC_COS_LAT
    return math.sqrt(dlat * dlat + dlon * dlon)


def fast_distance_sq_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Squared flat-earth distance (meters^2). Avoids sqrt — use for comparison only."""
    dlat = (lat2 - lat1) * _DEG_TO_M
    dlon = (lon2 - lon1) * _DEG_TO_M * _NYC_COS_LAT
    return dlat * dlat + dlon * dlon


def bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    lat_delta = radius_m / 111320.0
    lon_delta = radius_m / (111320.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def build_priority_weights(order: list[str], decay: float) -> dict[str, float]:
    weights: dict[str, float] = {}
    for idx, category in enumerate(order):
        weights[category] = round(decay ** idx, 4)
    return weights


def compact_records(rows: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return list(rows)[:limit]


def first_sentence(text: str, max_len: int = 200) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in text:
            return text.split(sep, 1)[0].strip()[:max_len]
    return text[:max_len]
