from __future__ import annotations


def _build_trend_summary(signal_name: str, trend: dict) -> str:
    raw = trend.get("raw_windows", {}) or {}
    quarterly = trend.get("quarterly_series", []) or []
    parts: list[str] = []
    recent = raw.get("last_30d")
    previous = raw.get("prev_30d")
    if recent is not None and previous is not None:
        change = trend.get("recent_delta", {}).get("change_pct")
        if previous == 0:
            parts.append(f"Last 30 days: {recent} event(s) (previous 30 days: {previous}).")
        elif change is not None:
            parts.append(f"Last 30 days: {recent} event(s) (previous 30 days: {previous}, change {change:+.1f}%).")
    current_90 = raw.get("last_90d")
    previous_90 = raw.get("same_90d_last_year")
    seasonal = trend.get("seasonal_delta", {}).get("change_pct")
    if current_90 is not None and previous_90 is not None and seasonal is not None:
        parts.append(f"Last 90 days: {current_90} event(s) (same 90 days last year: {previous_90}, change {seasonal:+.1f}%).")
    elif current_90 is not None and previous_90 is not None:
        parts.append(f"Last 90 days: {current_90} event(s) (same 90 days last year: {previous_90}).")
    if quarterly:
        quarter_chain = " -> ".join(f"{item['quarter']}: {item['count']}" for item in quarterly[-4:])
        parts.append(f"Quarterly progression: {quarter_chain}.")
    gap = trend.get("baseline_gap", {}).get("gap_pct")
    label = trend.get("baseline_gap", {}).get("percentile_label")
    if gap is not None:
        parts.append(f"Baseline gap: {gap:+.1f}% vs baseline ({label or 'unknown'}).")
    persistence = trend.get("persistence", {}).get("consecutive_above")
    if persistence:
        parts.append(f"Persistence: {persistence} consecutive elevated quarter(s).")
    anomaly = trend.get("anomaly", {})
    if anomaly.get("is_anomaly"):
        z = anomaly.get("z_score")
        ctx = anomaly.get("context", "")
        z_str = f" (z-score {z:+.2f})" if z is not None else ""
        parts.append(f"ANOMALY{z_str}: {ctx}.")
    return f"{signal_name}: " + " ".join(parts) if parts else f"{signal_name}: runtime trend summary unavailable."


def _pattern_summary(pattern: dict, evidence_by_id: dict[str, dict]) -> str:
    parts = [pattern.get("summary", "").strip()]
    related = []
    for evidence_id in pattern.get("evidence_ids", []):
        evidence = evidence_by_id.get(evidence_id)
        if evidence and evidence.get("summary"):
            related.append(evidence["summary"])
    if related:
        parts.append("Supporting signals: " + " ".join(related[:2]))
    return " ".join(part for part in parts if part).strip()


def build_evidence(query_evidence: list[dict], trends: dict, patterns: list[dict]) -> list[dict]:
    evidence = list(query_evidence)
    for signal_name, trend in trends.items():
        if any(
            [
                trend.get("recent_delta", {}).get("change_pct") is not None,
                trend.get("seasonal_delta", {}).get("change_pct") is not None,
                trend.get("baseline_gap", {}).get("gap_pct") is not None,
                trend.get("persistence", {}).get("consecutive_above"),
                trend.get("quarterly_series"),
            ]
        ):
            evidence.append({
                "evidence_id": f"trend_{signal_name}",
                "source": "Urban Dossier trend engine",
                "date": "runtime",
                "summary": _build_trend_summary(signal_name, trend),
                "record_ref": signal_name,
                "raw": {
                    "raw_windows": trend.get("raw_windows", {}),
                    "quarterly_series": trend.get("quarterly_series", []),
                },
            })
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    for pattern in patterns:
        evidence.append({
            "evidence_id": pattern["pattern_id"],
            "source": "Urban Dossier pattern detector",
            "date": "runtime",
            "summary": _pattern_summary(pattern, evidence_by_id),
            "record_ref": pattern["pattern_id"],
        })
    deduped = {item["evidence_id"]: item for item in evidence}
    return list(deduped.values())


def verify_priority_actions(priority_actions: list[dict], evidence_table: list[dict]) -> list[dict]:
    valid_ids = {item["evidence_id"] for item in evidence_table}
    return [
        item for item in priority_actions
        if all(eid in valid_ids for eid in item.get("evidence_ids", []))
    ]


def extract_why_now(trends: dict, patterns: list[dict]) -> list[dict]:
    items: list[dict] = []
    for signal_name, trend in trends.items():
        if trend.get("direction") not in {"worsening", "elevated"}:
            continue
        parts: list[str] = []
        recent = trend.get("recent_delta", {}).get("change_pct")
        seasonal = trend.get("seasonal_delta", {}).get("change_pct")
        raw = trend.get("raw_windows", {}) or {}
        if recent is not None and raw.get("last_30d") is not None and raw.get("prev_30d") is not None:
            parts.append(f"last 30 days {raw['last_30d']} vs previous 30 days {raw['prev_30d']} ({recent:+.1f}%)")
        if seasonal is not None and raw.get("last_90d") is not None and raw.get("same_90d_last_year") is not None:
            parts.append(f"last 90 days {raw['last_90d']} vs same period last year {raw['same_90d_last_year']} ({seasonal:+.1f}%)")
        if trend.get("persistence", {}).get("consecutive_above"):
            parts.append(f"{trend['persistence']['consecutive_above']} elevated quarter(s)")
        items.append({
            "signal": f"{signal_name} is {trend['direction']}" + (f" ({', '.join(parts)})" if parts else ""),
            "trend_type": trend["direction"],
            "evidence_ids": [f"trend_{signal_name}"],
        })
    for pattern in patterns:
        items.append({
            "signal": pattern["title"],
            "trend_type": "pattern",
            "evidence_ids": [pattern["pattern_id"]],
        })
    return items[:5]
