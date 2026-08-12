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

import json
import os
from datetime import datetime, timezone
from pathlib import Path
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
        # Review-found leaks: real tools return record lists under these
        # names, and the allowlist's stated failure mode ("a new sample key
        # slips through untrimmed, visibly") fired exactly as documented.
        "results",
        "evidence_table",
        "hits",
        "neighbors",
    }
)

# Where each filtered call is recorded -- EXPANSION_PLAN 3.3 requires a
# persisted audit record, not only a stamp inside the payload the model saw.
# JSONL append, one line per filtered tool call, in the state dir (never the
# repo). Auditing must never break the tool call it audits, hence the broad
# swallow.
AUDIT_PATH = Path(
    os.environ.get(
        "URBAN_DOSSIER_PAYLOAD_AUDIT",
        "/mnt/data/urban-dossier-state/runtime/agent_payload_audit.jsonl",
    )
)


def audit_record(tool: str, policy: "PayloadPolicy", omitted: int) -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": tool,
                "policy": policy.value,
                "omitted_records": omitted,
            }) + "\n")
    except OSError:  # pragma: no cover - audit failure must not break the call
        pass


def count_omitted(filtered) -> int:
    if isinstance(filtered, dict):
        if set(filtered) == {"omitted_records", "reason"}:
            return int(filtered["omitted_records"])
        return sum(count_omitted(v) for v in filtered.values())
    if isinstance(filtered, list):
        return sum(count_omitted(v) for v in filtered)
    return 0


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
