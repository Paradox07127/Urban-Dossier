"""Canonical calendar-period helpers shared by trends and map timelines."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


_QUARTER_RE = re.compile(r"^(?P<year>\d{4})-?Q(?P<quarter>[1-4])$", re.IGNORECASE)
MIN_DATA_YEAR = 2000


def quarter_index(period: str) -> int:
    """Return a sortable integer for an already canonical ``YYYY-Qn`` key."""

    match = _QUARTER_RE.fullmatch(period)
    if not match:
        raise ValueError("quarter must use YYYY-Qn")
    return int(match.group("year")) * 4 + int(match.group("quarter")) - 1


def current_quarter(as_of: date | None = None) -> str:
    observed = as_of or date.today()
    return f"{observed.year}-Q{((observed.month - 1) // 3) + 1}"


def canonical_quarter(
    value: Any,
    *,
    as_of: date | None = None,
    min_year: int = MIN_DATA_YEAR,
) -> str | None:
    """Validate and normalize an artifact key without inventing a period.

    Both ``YYYYQn`` (Gold artifact storage) and ``YYYY-Qn`` (public API) are
    accepted. Pre-2000 placeholders, malformed years, and future quarters are
    rejected because the current datasets describe observed events.
    """

    if not isinstance(value, str):
        return None
    match = _QUARTER_RE.fullmatch(value.strip())
    if not match:
        return None
    year = int(match.group("year"))
    quarter = int(match.group("quarter"))
    canonical = f"{year:04d}-Q{quarter}"
    if year < min_year or quarter_index(canonical) > quarter_index(current_quarter(as_of)):
        return None
    return canonical


def quarter_for_date(value: date | datetime) -> str:
    return f"{value.year:04d}-Q{((value.month - 1) // 3) + 1}"


def is_consecutive_quarter(earlier: str, later: str) -> bool:
    return quarter_index(later) - quarter_index(earlier) == 1
