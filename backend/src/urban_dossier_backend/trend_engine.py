from __future__ import annotations

from typing import Any

from .periods import canonical_quarter, current_quarter, is_consecutive_quarter, quarter_index


TREND_SIGNALS = {
    "collision": {"current_module": "transit", "current_key": "collision_count_500m", "threshold": 25},
    "rodent": {"current_module": "safety", "current_key": "rodent_positive_500m", "threshold": 8},
    "311_sanitation": {"current_module": "safety", "current_key": "sanitation_311_recent_count", "threshold": 12},
    "ems_response": {"current_module": "safety", "current_key": "ems_avg_response_seconds"},
    "fire_response": {"current_module": "safety", "current_key": "fire_avg_response_seconds"},
    "housing_violations": {"current_module": "building", "current_key": "open_violations_total_250m", "threshold": 10},
}


def _coerce_number(value: Any) -> float:
    """Coerce values_by_period entries to a float.

    The ready-quarterly provider intentionally sets last_30d/prev_30d/etc to None
    when only quarterly aggregates are available. dict.get(key, default) only
    applies the default when the key is absent, not when the value is None,
    so we normalise here to avoid TypeError in arithmetic below.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compute_recent_delta(values_by_period: dict[str, Any]) -> dict[str, Any]:
    recent = _coerce_number(values_by_period.get("last_30d"))
    previous = _coerce_number(values_by_period.get("prev_30d"))
    if previous == 0:
        return {"change_pct": None, "label": "no_baseline"}
    pct = (recent - previous) / previous * 100
    return {"change_pct": round(pct, 1), "label": "rising" if pct > 10 else "falling" if pct < -10 else "stable"}


def compute_seasonal_delta(values_by_period: dict[str, Any]) -> dict[str, Any]:
    current = _coerce_number(values_by_period.get("last_90d"))
    same_last_year = _coerce_number(values_by_period.get("same_90d_last_year"))
    if same_last_year == 0:
        return {"change_pct": None, "label": "no_baseline"}
    pct = (current - same_last_year) / same_last_year * 100
    return {"change_pct": round(pct, 1), "label": "rising" if pct > 15 else "falling" if pct < -15 else "stable"}


def compute_baseline_gap(current_value: float | int | None, baseline_dist: dict[str, Any]) -> dict[str, Any]:
    if current_value is None:
        return {"gap_pct": None, "percentile_label": "unknown"}
    p50 = baseline_dist.get("p50")
    if not p50:
        return {"gap_pct": None, "percentile_label": "unknown"}
    gap = (float(current_value) - float(p50)) / float(p50) * 100
    if current_value <= baseline_dist.get("p25", current_value):
        label = "below_average"
    elif current_value <= baseline_dist.get("p50", current_value):
        label = "average"
    elif current_value <= baseline_dist.get("p75", current_value):
        label = "above_average"
    else:
        label = "high"
    return {"gap_pct": round(gap, 1), "percentile_label": label}


def compute_persistence(values_by_period: dict[str, Any], threshold: float | int | None = None) -> dict[str, Any]:
    periods = [
        point for point in _build_quarterly_series(values_by_period)
        if point["period_complete"]
    ]
    result = {
        "consecutive_above": 0,
        "method": "consecutive_calendar_quarters_above_threshold",
        "threshold": threshold,
        "n_observations": len(periods),
        "missing_data_policy": "a missing calendar quarter breaks the run",
    }
    if not periods or threshold is None:
        return result
    count = 0
    later_period: str | None = None
    for point in reversed(periods):
        period = point["period"]
        value = point["value"]
        if later_period is not None and not is_consecutive_quarter(period, later_period):
            break
        if value is None:
            break
        if value > threshold:
            count += 1
            later_period = period
        else:
            break
    result["consecutive_above"] = count
    return result


def compute_anomaly(values_by_period: dict[str, Any]) -> dict[str, Any]:
    """Detect if the latest quarter is a statistical anomaly vs historical baseline.

    Uses z-score over prior quarters. Requires >= 4 quarters of history so the
    baseline is meaningful. The latest quarter is excluded from the baseline.
    """
    quarterly = [
        point for point in _build_quarterly_series(values_by_period)
        if point["period_complete"]
    ]
    usable = [point for point in quarterly if point["value"] is not None]
    metadata = {
        "method": "z_score_latest_vs_prior_observed_quarters_population_std",
        "minimum_observations": 4,
        "n_observations": len(usable),
        "missing_data_policy": "listwise exclusion by real period key; partial current quarter excluded",
    }
    if len(usable) < 4:
        return {
            "is_anomaly": False,
            "z_score": None,
            "context": "insufficient_history",
            **metadata,
        }

    baseline = [point["value"] for point in usable[:-1]]
    latest_point = usable[-1]
    latest = latest_point["value"]

    mean = sum(baseline) / len(baseline)
    variance = sum((x - mean) ** 2 for x in baseline) / len(baseline)
    std = variance ** 0.5

    if std == 0:
        is_different = latest != mean
        return {
            "is_anomaly": is_different,
            "z_score": None,
            "context": f"zero variance baseline ({mean:.1f}), latest={latest}" if is_different else "no_variance",
            "latest_period": latest_point["period"],
            **metadata,
        }

    z = (latest - mean) / std
    is_anomaly = abs(z) > 1.5

    if z > 1.5:
        context = f"latest quarter ({latest}) significantly above historical mean ({mean:.1f} ± {std:.1f})"
    elif z < -1.5:
        context = f"latest quarter ({latest}) significantly below historical mean ({mean:.1f} ± {std:.1f})"
    else:
        context = "within_normal_range"

    return {
        "is_anomaly": is_anomaly,
        "z_score": round(z, 2),
        "context": context,
        "latest_period": latest_point["period"],
        **metadata,
    }


def determine_direction(signal_name: str, trend: dict[str, Any]) -> str:
    recent = trend.get("recent_delta", {}).get("change_pct")
    seasonal = trend.get("seasonal_delta", {}).get("change_pct")
    baseline = trend.get("baseline_gap", {}).get("gap_pct")
    persistence = trend.get("persistence", {}).get("consecutive_above", 0)

    if signal_name in {"collision", "rodent", "311_sanitation", "ems_response", "fire_response", "housing_violations"}:
        if (recent is not None and recent > 10) or (seasonal is not None and seasonal > 15) or persistence >= 2:
            return "worsening"
        if baseline is not None and baseline > 30:
            return "elevated"
    if recent is None and seasonal is None and baseline is None and persistence == 0:
        return "insufficient_data"
    return "stable"


def _build_quarterly_series(values_by_period: dict[str, Any]) -> list[dict[str, Any]]:
    by_period: dict[str, dict[str, Any]] = {}
    for raw in values_by_period.get("quarterly_series", []) or []:
        if not isinstance(raw, dict):
            continue
        period = canonical_quarter(raw.get("period"))
        value = raw.get("value")
        if period is None or (value is not None and not isinstance(value, (int, float))):
            continue
        coverage = raw.get("coverage")
        by_period[period] = {
            "period": period,
            "value": value,
            "coverage": coverage if isinstance(coverage, (int, float)) else None,
            "period_complete": (
                raw.get("period_complete")
                if isinstance(raw.get("period_complete"), bool)
                else period != current_quarter()
            ),
        }
    return [by_period[period] for period in sorted(by_period, key=quarter_index)]


def compute_all_trends(current_state: dict[str, Any], historical_queries: dict[str, Any], baselines: dict[str, Any]) -> dict[str, Any]:
    trends: dict[str, Any] = {}
    for signal_name, config in TREND_SIGNALS.items():
        values = historical_queries.get(signal_name, {})
        current_value = current_state.get(config["current_module"], {}).get(config["current_key"])
        trend = {
            "recent_delta": compute_recent_delta(values),
            "seasonal_delta": compute_seasonal_delta(values),
            "baseline_gap": compute_baseline_gap(current_value, baselines.get(signal_name, {})),
            "persistence": compute_persistence(values, config.get("threshold")),
            "anomaly": compute_anomaly(values),
            "raw_windows": {
                "last_30d": values.get("last_30d"),
                "prev_30d": values.get("prev_30d"),
                "last_90d": values.get("last_90d"),
                "same_90d_last_year": values.get("same_90d_last_year"),
            },
            "quarterly_series": _build_quarterly_series(values),
            "quarterly_methodology": {
                "period_key": "YYYY-Qn",
                "alignment": "calendar key",
                "missing_data_policy": (
                    "missing periods remain missing; no positional padding; "
                    "partial current quarter excluded from statistics"
                ),
            },
        }
        trend["direction"] = determine_direction(signal_name, trend)
        trends[signal_name] = trend
    return trends
