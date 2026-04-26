"""Statistical multi-signal pattern detector (v2 clean rewrite).

This module replaces the v1 hardcoded pair-based detector with a three-layer
pipeline:

  Layer 1 -- Spearman rank correlation across ALL signal pairs, with a
             Bonferroni correction over the number of tested pairs.
  Layer 2 -- Trend co-direction filter (only retain pairs where both signals
             have a worsening or elevated trend, per trend_engine output).
  Layer 3 -- Nemotron / vLLM naming. The model produces a concise pattern
             title plus a one-sentence explanation, or rejects the pattern
             as not operationally meaningful.

The public API (``detect_multi_signal_patterns``) is preserved so existing
service.py / evidence.py consumers continue to work. New per-pattern fields
``correlation_coefficient``, ``p_value`` and ``llm_confidence`` are added
without breaking the legacy contract.

Runtime target: NVIDIA DGX Spark (GB10, ARM64). vLLM is expected to be
reachable on ``http://localhost:8000/v1`` serving the model named in
``URBAN_DOSSIER_MODEL``. If vLLM is unreachable Layer 3 is skipped and
auto-generated titles are used; the pipeline never fails the caller.
"""

from __future__ import annotations

import json
import logging
import os
from itertools import combinations
from typing import Any, Iterable

from scipy import stats  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Minimum number of paired observations needed for a Spearman test to be
# considered. Below this we silently skip the pair.
_MIN_OBS = 4

# Strength threshold on |rho| applied jointly with the Bonferroni-corrected
# significance test.
_RHO_THRESHOLD = 0.6

# Family-wise alpha applied across all tested pairs.
_FAMILY_ALPHA = 0.01

# Trend directions that count as "stressed" -- must match trend_engine.py.
_STRESSED_DIRECTIONS = {"worsening", "elevated"}

# vLLM naming endpoint configuration.
_VLLM_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
_VLLM_API_KEY = os.getenv("OPENAI_API_KEY", "not-needed")
_VLLM_MODEL = os.getenv(
    "URBAN_DOSSIER_MODEL",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4",
)
_VLLM_TIMEOUT_SECONDS = float(os.getenv("URBAN_DOSSIER_LLM_TIMEOUT", "8"))


# ---------------------------------------------------------------------------
# Layer helpers
# ---------------------------------------------------------------------------


def _extract_signal_series(trends: dict[str, Any]) -> dict[str, list[float]]:
    """Pull a comparable numeric series for each signal from trends.

    The trend engine emits a ``quarterly_series`` list of dicts with a
    ``count`` key per quarter. That series is the most reliable cross-signal
    payload because it has consistent length when the upstream data is
    available. Signals without enough history are dropped here.
    """
    series_by_signal: dict[str, list[float]] = {}
    for signal_name, trend in (trends or {}).items():
        if not isinstance(trend, dict):
            continue
        quarterly = trend.get("quarterly_series") or []
        values: list[float] = []
        for entry in quarterly:
            if not isinstance(entry, dict):
                continue
            value = entry.get("count")
            if value is None:
                continue
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        if len(values) >= _MIN_OBS:
            series_by_signal[signal_name] = values
    return series_by_signal


def _aligned_pair(a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
    """Trim two series from the right so they share the most recent N points."""
    n = min(len(a), len(b))
    return a[-n:], b[-n:]


def _layer1_correlations(series_by_signal: dict[str, list[float]]) -> list[dict[str, Any]]:
    """Compute Spearman correlations across all signal pairs with Bonferroni."""
    signals = sorted(series_by_signal.keys())
    pairs = list(combinations(signals, 2))
    n_pairs = len(pairs)
    if n_pairs == 0:
        return []
    effective_alpha = _FAMILY_ALPHA / n_pairs

    significant: list[dict[str, Any]] = []
    for sig_a, sig_b in pairs:
        a, b = _aligned_pair(series_by_signal[sig_a], series_by_signal[sig_b])
        if len(a) < _MIN_OBS:
            continue
        try:
            result = stats.spearmanr(a, b)
            rho = float(result.correlation)
            p_value = float(result.pvalue)
        except Exception as exc:  # numerical degeneracies, all-equal vectors, etc.
            logger.debug("spearman failed for %s/%s: %s", sig_a, sig_b, exc)
            continue
        if rho != rho or p_value != p_value:  # NaN guard (no math import needed)
            continue
        if abs(rho) > _RHO_THRESHOLD and p_value < effective_alpha:
            significant.append(
                {
                    "signal_a": sig_a,
                    "signal_b": sig_b,
                    "rho": rho,
                    "p_value": p_value,
                    "n_observations": len(a),
                    "effective_alpha": effective_alpha,
                }
            )
    return significant


def _layer2_filter_codirectional(
    candidates: list[dict[str, Any]], trends: dict[str, Any]
) -> list[dict[str, Any]]:
    """Keep only pairs where both signals have a stressed trend direction."""
    surviving: list[dict[str, Any]] = []
    for candidate in candidates:
        dir_a = (trends.get(candidate["signal_a"], {}) or {}).get("direction")
        dir_b = (trends.get(candidate["signal_b"], {}) or {}).get("direction")
        if dir_a in _STRESSED_DIRECTIONS and dir_b in _STRESSED_DIRECTIONS:
            candidate["direction_a"] = dir_a
            candidate["direction_b"] = dir_b
            surviving.append(candidate)
    return surviving


# ---------------------------------------------------------------------------
# Layer 3 -- LLM naming via vLLM (Nemotron)
# ---------------------------------------------------------------------------


def _format_signal_for_llm(signal: str, trend: dict[str, Any]) -> str:
    """Compact human-readable description of a single signal's recent trend."""
    raw = trend.get("raw_windows", {}) or {}
    parts: list[str] = [f"{signal} ({trend.get('direction', 'unknown')})"]
    last_30 = raw.get("last_30d")
    prev_30 = raw.get("prev_30d")
    if last_30 is not None and prev_30 is not None:
        parts.append(f"30d={last_30} vs prev_30d={prev_30}")
    last_90 = raw.get("last_90d")
    same_90_ly = raw.get("same_90d_last_year")
    if last_90 is not None and same_90_ly is not None:
        parts.append(f"90d={last_90} vs same_period_last_year={same_90_ly}")
    return "; ".join(parts)


def _call_vllm_naming(pair: dict[str, Any], trends: dict[str, Any]) -> dict[str, Any] | None:
    """Ask Nemotron via vLLM to name the pattern. Returns parsed JSON or None.

    Returns ``None`` if the LLM is unreachable, errors, or returns malformed
    output. A returned dict may contain ``{"reject": true}`` which the caller
    interprets as "the model declined to name this pair".
    """
    try:
        from openai import OpenAI  # local import to keep module import light
    except ImportError as exc:  # pragma: no cover -- handled at import time on DGX
        logger.warning("openai SDK unavailable, skipping LLM naming: %s", exc)
        return None

    snippet_a = _format_signal_for_llm(pair["signal_a"], trends.get(pair["signal_a"], {}))
    snippet_b = _format_signal_for_llm(pair["signal_b"], trends.get(pair["signal_b"], {}))
    rho = pair["rho"]
    p_value = pair["p_value"]

    user_prompt = (
        "Given two correlated urban operations signals showing the data below, "
        "propose a concise pattern name (<= 8 words) and a 1-sentence explanation "
        "of what the joint movement implies for a city operations team. "
        "If no operationally meaningful pattern exists, respond with "
        '{"reject": true}.\n\n'
        f"Signal A: {snippet_a}\n"
        f"Signal B: {snippet_b}\n"
        f"Spearman rho={rho:.3f}, p={p_value:.2e}\n\n"
        "Respond with strict JSON of the form:\n"
        '{"title": "...", "explanation": "...", "severity": "high|medium|low"} '
        'OR {"reject": true}.'
    )

    try:
        client = OpenAI(base_url=_VLLM_BASE_URL, api_key=_VLLM_API_KEY, timeout=_VLLM_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=_VLLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an urban operations analyst. Always reply with a single JSON object. "
                        "Do not wrap JSON in code fences."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=256,
        )
    except Exception as exc:
        logger.warning("vLLM naming call failed for %s/%s: %s", pair["signal_a"], pair["signal_b"], exc)
        return None

    try:
        content = response.choices[0].message.content or ""
        parsed = json.loads(content)
    except (AttributeError, IndexError, json.JSONDecodeError) as exc:
        logger.warning("vLLM returned non-JSON content for %s/%s: %s", pair["signal_a"], pair["signal_b"], exc)
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def _evidence_ids_for(signal: str) -> list[str]:
    """Best-effort evidence id list -- mirrors the convention used by evidence.py.

    evidence.py builds ``trend_<signal>`` ids; downstream report code may also
    look for raw signal ids. We emit both forms.
    """
    return [signal, f"trend_{signal}"]


def _auto_title(pair: dict[str, Any]) -> str:
    """Fallback title used when LLM is unreachable or rejects the pair."""
    direction = "co-stressed" if pair.get("direction_a") == pair.get("direction_b") else "diverging stress"
    return f"{pair['signal_a']} and {pair['signal_b']} are {direction} (rho={pair['rho']:+.2f})"


def _auto_summary(pair: dict[str, Any]) -> str:
    return (
        f"Spearman correlation of {pair['rho']:+.3f} between {pair['signal_a']} "
        f"and {pair['signal_b']} (p={pair['p_value']:.2e}, n={pair['n_observations']}). "
        f"Both signals trend {pair.get('direction_a', 'n/a')} / {pair.get('direction_b', 'n/a')}."
    )


def _severity_from_rho(rho: float) -> str:
    magnitude = abs(rho)
    if magnitude >= 0.85:
        return "high"
    if magnitude >= 0.7:
        return "medium"
    return "low"


def _assemble_pattern(pair: dict[str, Any], llm: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the final pattern dict, honouring an LLM rejection if present."""
    if llm is not None and llm.get("reject") is True:
        return None  # model declined; drop the pattern

    if llm is not None and isinstance(llm.get("title"), str):
        title = llm["title"].strip() or _auto_title(pair)
        summary = (llm.get("explanation") or "").strip() or _auto_summary(pair)
        severity = llm.get("severity") if llm.get("severity") in {"high", "medium", "low"} else _severity_from_rho(pair["rho"])
        confidence = 1
    else:
        title = _auto_title(pair)
        summary = _auto_summary(pair)
        severity = _severity_from_rho(pair["rho"])
        confidence = 0

    pattern_id = f"pattern_{pair['signal_a']}_{pair['signal_b']}"
    evidence_ids = _evidence_ids_for(pair["signal_a"]) + _evidence_ids_for(pair["signal_b"])

    return {
        "pattern_id": pattern_id,
        "title": title,
        "summary": summary,
        "evidence_ids": evidence_ids,
        "severity": severity,
        "correlation_coefficient": round(pair["rho"], 4),
        "p_value": float(pair["p_value"]),
        "llm_confidence": confidence,
    }


def _dedupe_patterns(patterns: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for pattern in patterns:
        pid = pattern["pattern_id"]
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pattern)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_multi_signal_patterns(current_state: dict, trends: dict) -> list[dict]:
    """Detect cross-signal patterns and return enriched pattern dicts.

    The output contract preserves v1's ``pattern_id``, ``title``, ``summary``,
    ``evidence_ids`` and ``severity`` fields so downstream consumers
    (service.py, evidence.py, report.py) keep working unchanged. Three new
    fields are added: ``correlation_coefficient``, ``p_value`` and
    ``llm_confidence`` (1 if the LLM successfully named the pattern, 0 if a
    fallback title was used).
    """
    series_by_signal = _extract_signal_series(trends)
    if len(series_by_signal) < 2:
        logger.info(
            "pattern_detector: insufficient signals (%d) with >= %d quarters of data",
            len(series_by_signal),
            _MIN_OBS,
        )
        return []

    layer1 = _layer1_correlations(series_by_signal)
    if not layer1:
        logger.info("pattern_detector: 0/%d candidate pairs survived Layer 1", len(series_by_signal))
        return []

    layer2 = _layer2_filter_codirectional(layer1, trends)
    if not layer2:
        logger.info(
            "pattern_detector: %d Layer 1 candidates dropped by Layer 2 trend filter",
            len(layer1),
        )
        return []

    rejected = 0
    named = 0
    fallback = 0
    patterns: list[dict[str, Any]] = []
    for pair in layer2:
        llm_response = _call_vllm_naming(pair, trends)
        if llm_response is not None and llm_response.get("reject") is True:
            rejected += 1
            continue
        pattern = _assemble_pattern(pair, llm_response)
        if pattern is None:
            rejected += 1
            continue
        if pattern["llm_confidence"] == 1:
            named += 1
        else:
            fallback += 1
        patterns.append(pattern)

    logger.info(
        "pattern_detector: layer1=%d layer2=%d named=%d fallback=%d rejected=%d",
        len(layer1),
        len(layer2),
        named,
        fallback,
        rejected,
    )
    return _dedupe_patterns(patterns)
