"""How much data the model is shown per tool call -- EXPANSION_PLAN 3.3.

Three explicit tiers, from the Analyst-copilot pattern the plan adopts:

``schema_only``          shapes, not values: every leaf becomes its type name,
                         lists become their length. For probing what a tool
                         returns without exposing any reading.
``schema_aggregates``    scalars and small aggregate values pass; record lists
                         (samples, incident rows) are replaced by an explicit
                         omission marker carrying the count. The model knows
                         how much it is not seeing.
``schema_aggregates_sample``  full payloads -- today's behaviour, and the
                         default, so turning this feature on changes nothing
                         until someone chooses a stricter tier.

The tier is resolved once per process from ``URBAN_DOSSIER_PAYLOAD_POLICY``
and stamped into every filtered payload under ``payload_policy``, which is
what makes an agent run auditable: each tool result in the trace states the
visibility regime it was produced under, rather than leaving reviewers to
guess what the model could see.

Sample-bearing keys are named explicitly. A heuristic ("any list of dicts is
a sample") would silently eat structures that are genuinely aggregates --
histogram buckets, chart specs, class breaks -- and the failure mode of an
allowlist (a new sample key slips through untrimmed) is visible and fixable,
while the failure mode of a heuristic (an aggregate silently vanishes) is
neither.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Any


class PayloadPolicy(str, Enum):
    SCHEMA_ONLY = "schema_only"
    SCHEMA_AGGREGATES = "schema_aggregates"
    SCHEMA_AGGREGATES_SAMPLE = "schema_aggregates_sample"


DEFAULT_POLICY = PayloadPolicy.SCHEMA_AGGREGATES_SAMPLE

# Keys whose values are row-level samples rather than aggregates. Extend when
# a tool grows a new sample field; the test pins the known ones.
SAMPLE_KEYS = frozenset(
    {
        "recent_incidents",
        "rows",
        "sample",
        "samples",
        "records",
        "matches",
        "building_flags",
        "evidence_rows",
    }
)


def resolve_policy(env: dict[str, str] | None = None) -> PayloadPolicy:
    """The active tier. Unknown or empty values fall back to the default --
    a typo must not silently tighten (breaking the agent) or loosen policy."""
    raw = (env or os.environ).get("URBAN_DOSSIER_PAYLOAD_POLICY", "").strip().lower()
    try:
        return PayloadPolicy(raw) if raw else DEFAULT_POLICY
    except ValueError:
        return DEFAULT_POLICY


def _schema_of(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _schema_of(item) for key, item in value.items()}
    if isinstance(value, list):
        return f"[{len(value)} items]"
    return type(value).__name__


def _strip_samples(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in SAMPLE_KEYS and isinstance(item, list):
                out[key] = {"omitted_records": len(item), "reason": "payload_policy"}
            else:
                out[key] = _strip_samples(item)
        return out
    if isinstance(value, list):
        return [_strip_samples(item) for item in value]
    return value


def apply_policy(payload: dict[str, Any], policy: PayloadPolicy) -> dict[str, Any]:
    """Filter one tool result to the tier, stamping the tier on the output.

    Never mutates the input; the unfiltered result stays whole for the layers
    (session store, HTTP response) that are allowed to see it.
    """
    if policy is PayloadPolicy.SCHEMA_AGGREGATES_SAMPLE:
        return {**payload, "payload_policy": policy.value}
    if policy is PayloadPolicy.SCHEMA_AGGREGATES:
        return {**_strip_samples(payload), "payload_policy": policy.value}
    return {**_schema_of(payload), "payload_policy": policy.value}
