"""The intent gate and the payload policy -- EXPANSION_PLAN 3.4 and 3.3.

The router's contract is determinism plus conservatism: same message, same
route, and nothing ambiguous ever short-circuits. The policy's contract is
that stricter tiers only ever remove information, announce what they removed,
and stamp every payload with the regime it passed through.
"""
from __future__ import annotations

import pytest

from urban_dossier_analyst.payload_policy import (
    DEFAULT_POLICY,
    PayloadPolicy,
    apply_policy,
    resolve_policy,
)
from urban_dossier_analyst.routing import Intent, route_intent


# --- routing: out of scope ---------------------------------------------------


@pytest.mark.parametrize("message", [
    "write me a python function to sort a list",
    "帮我写一段代码修复这个 bug",
    "compose a poem about the subway",
    "what's the weather forecast tomorrow?",
    "what do you think about the election?",
    "tell me about restaurants in Boston",
])
def test_unmistakably_off_topic_is_refused(message):
    assert route_intent(message).intent is Intent.OUT_OF_SCOPE


def test_weather_near_a_covered_place_falls_through_to_the_agent():
    """Borderline cases go to the agent, which can disappoint with context."""
    assert route_intent("weather near Astoria today?").intent is Intent.NEW_ANALYSIS


# --- routing: meta help ------------------------------------------------------


@pytest.mark.parametrize("message", [
    "what can you do?",
    "你有什么功能",
    "which datasets do you use?",
    "help",
])
def test_capability_questions_get_the_canned_answer(message):
    assert route_intent(message).intent is Intent.META_HELP


def test_a_place_question_shaped_like_meta_is_analysis():
    assert route_intent("what can you tell me about Williamsburg?").intent is Intent.NEW_ANALYSIS


# --- routing: evidence follow-ups --------------------------------------------


def test_a_why_probe_with_history_reads_from_evidence():
    assert route_intent("why is that score so low?", has_history=True).intent \
        is Intent.ASK_FROM_EVIDENCE


def test_the_same_probe_without_history_is_fresh_analysis():
    """Nothing to refer back to -- 'that score' cannot resolve."""
    assert route_intent("why is that score so low?", has_history=False).intent \
        is Intent.NEW_ANALYSIS


def test_a_new_location_beats_evidence_anaphora():
    route = route_intent("why is that score lower near Astoria?", has_history=True)
    assert route.intent is Intent.NEW_ANALYSIS


def test_ambiguity_defaults_to_analysis():
    assert route_intent("how safe is the east village at night").intent is Intent.NEW_ANALYSIS


def test_routes_are_deterministic_and_carry_their_rule():
    a = route_intent("write code to parse csv")
    b = route_intent("write code to parse csv")
    assert a == b
    assert a.rule == "code_request"


# --- payload policy ----------------------------------------------------------


PAYLOAD = {
    "score": 62,
    "counts": {"collision_count_500m": 31},
    "recent_incidents": [{"kind": "collision", "date": "2026-01-02"}] * 7,
    "nested": {"rows": [{"a": 1}, {"a": 2}], "aggregate_mean": 4.5},
    "note": "text",
}


def test_default_tier_changes_nothing_but_the_stamp():
    out = apply_policy(PAYLOAD, PayloadPolicy.SCHEMA_AGGREGATES_SAMPLE)
    assert out["recent_incidents"] == PAYLOAD["recent_incidents"]
    assert out["payload_policy"] == "schema_aggregates_sample"


def test_aggregates_tier_replaces_samples_with_counted_omissions():
    out = apply_policy(PAYLOAD, PayloadPolicy.SCHEMA_AGGREGATES)
    assert out["recent_incidents"] == {"omitted_records": 7, "reason": "payload_policy"}
    assert out["nested"]["rows"] == {"omitted_records": 2, "reason": "payload_policy"}
    # aggregates survive untouched
    assert out["counts"]["collision_count_500m"] == 31
    assert out["nested"]["aggregate_mean"] == 4.5
    assert out["payload_policy"] == "schema_aggregates"


def test_schema_tier_exposes_shapes_not_values():
    out = apply_policy(PAYLOAD, PayloadPolicy.SCHEMA_ONLY)
    assert out["score"] == "int"
    assert out["recent_incidents"] == "[7 items]"
    assert out["counts"] == {"collision_count_500m": "int"}
    assert out["payload_policy"] == "schema_only"


def test_input_payload_is_never_mutated():
    before = str(PAYLOAD)
    apply_policy(PAYLOAD, PayloadPolicy.SCHEMA_AGGREGATES)
    apply_policy(PAYLOAD, PayloadPolicy.SCHEMA_ONLY)
    assert str(PAYLOAD) == before


def test_unknown_env_value_falls_back_to_default_not_to_strictest():
    """A typo must not strangle the agent (or loosen a chosen policy)."""
    assert resolve_policy({"URBAN_DOSSIER_PAYLOAD_POLICY": "scheema_only"}) is DEFAULT_POLICY
    assert resolve_policy({}) is DEFAULT_POLICY
    assert resolve_policy({"URBAN_DOSSIER_PAYLOAD_POLICY": "schema_only"}) \
        is PayloadPolicy.SCHEMA_ONLY


def test_dispatch_stamps_the_policy_on_real_tool_errors_never():
    """Errors bypass the policy: an error string carries no data rows and the
    model needs it verbatim. Only successful payloads are filtered."""
    from urban_dossier_analyst.tools import dispatch_tool

    out = dispatch_tool("no_such_tool", {})
    assert "error" in out and "payload_policy" not in out
