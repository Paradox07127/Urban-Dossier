"""Regression tests for reasoning-model handling in the ReAct loop.

Background: /api/agent/ask returned HTTP 200 with the text "Agent loop
terminated without producing a final answer" for every question that touched a
tool. Two causes, both exercised here:

  1. Nemotron puts its chain of thought in ``reasoning`` and the answer in
     ``content``. A response cut off mid-reasoning has ``content = None``, and
     the loop read ``content`` only, so a truncated turn looked like a
     finished-but-empty one.
  2. ``score_neighborhood`` returns ~59k chars (~15k tokens). Fed back verbatim
     into a 32k context it left so little room that the model hit its token
     limit while still reasoning -- producing exactly the empty response in (1).

These use the ``client_factory`` seam and need neither vLLM nor the backend.
"""

from __future__ import annotations

import json

import pytest

from urban_dossier_analyst.agent_loop import (
    _compact_observation,
    _message_text,
    resolve_sampling,
    run_agent,
)


# --------------------------------------------------------------------------- #
# _message_text
# --------------------------------------------------------------------------- #


def test_message_text_prefers_content():
    assert _message_text({"content": "the answer", "reasoning": "thinking"}) == "the answer"


def test_message_text_falls_back_to_reasoning_when_content_is_none():
    """A truncated reasoning turn must still surface something to the user."""

    assert _message_text({"content": None, "reasoning": "partial analysis"}) == "partial analysis"


def test_message_text_accepts_legacy_reasoning_content_field():
    assert _message_text({"content": "", "reasoning_content": "older vllm build"}) == (
        "older vllm build"
    )


def test_message_text_returns_empty_when_nothing_usable():
    assert _message_text({"content": None, "reasoning": None}) == ""
    assert _message_text({"content": "   "}) == ""


# --------------------------------------------------------------------------- #
# _compact_observation
# --------------------------------------------------------------------------- #


def test_small_observation_is_passed_through_untouched():
    result = {"scores": {"safety": 35}, "target": "east village"}

    assert json.loads(_compact_observation(result, 8000)) == result


def test_oversized_observation_drops_largest_fields_and_reports_them():
    result = {
        "scores": {"safety": 35},
        "target": {"lat": 40.7},
        "evidence_table": "x" * 20000,
        "trends": "y" * 16000,
        "why_now": "short",
    }

    compacted = json.loads(_compact_observation(result, 4000))

    assert len(json.dumps(compacted)) <= 4000 + 200  # payload plus the note
    assert "evidence_table" in compacted["_omitted_fields"]
    assert "trends" in compacted["_omitted_fields"]
    # The fields carrying the actual answer survive.
    assert compacted["scores"] == {"safety": 35}
    assert compacted["why_now"] == "short"


def test_scores_and_target_are_never_dropped_even_when_huge():
    """Protected fields hold the answer; losing them defeats the tool call."""

    result = {"scores": {"safety": 35}, "target": "t" * 9000}

    compacted = json.loads(_compact_observation(result, 1000))

    assert compacted["scores"] == {"safety": 35}
    assert compacted["target"] == "t" * 9000


def test_error_payloads_survive_compaction():
    """dispatch_tool signals failure via error/retry_hint - the model needs both."""

    result = {"error": "boom", "retry_hint": "try again", "junk": "z" * 9000}

    compacted = json.loads(_compact_observation(result, 500))

    assert compacted["error"] == "boom"
    assert compacted["retry_hint"] == "try again"


# --------------------------------------------------------------------------- #
# Truncation handling inside run_agent
# --------------------------------------------------------------------------- #


class _StubClient:
    """Replays a scripted list of (content, reasoning, finish_reason, tools)."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []
        outer = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                content, reasoning, finish, tools = outer._script.pop(0)
                message = type(
                    "Msg",
                    (),
                    {
                        "model_dump": lambda self: {
                            "content": content,
                            "reasoning": reasoning,
                            "tool_calls": tools,
                        }
                    },
                )()
                choice = type("Choice", (), {"message": message, "finish_reason": finish})()
                return type("Resp", (), {"choices": [choice]})()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def test_truncated_empty_turn_triggers_a_bounded_wrapup_call():
    """finish_reason='length' with no content means cut off, not finished."""

    client = _StubClient(
        [
            (None, "I was still reasoning when I ran out of room", "length", None),
            ("Final: safety score is 35.", None, "stop", None),
        ]
    )

    result = run_agent(user_message="q", client_factory=lambda _u: client)

    assert result["answer"] == "Final: safety score is 35."
    # The wrap-up must be bounded so it cannot truncate the same way.
    assert client.calls[-1]["max_tokens"] == 1024
    assert client.calls[-1]["tool_choice"] == "none"


def test_normal_stop_with_content_does_not_trigger_a_wrapup():
    client = _StubClient([("Direct answer.", "some thinking", "stop", None)])

    result = run_agent(user_message="q", client_factory=lambda _u: client)

    assert result["answer"] == "Direct answer."
    assert len(client.calls) == 1


def test_truncated_turn_that_still_has_content_is_kept():
    """Don't discard a usable partial answer just because it was cut off."""

    client = _StubClient([("Partial but usable answer", None, "length", None)])

    result = run_agent(user_message="q", client_factory=lambda _u: client)

    assert result["answer"] == "Partial but usable answer"
    assert len(client.calls) == 1


def test_oversized_tool_result_is_compacted_before_reaching_the_model():
    """The model sees a reduced observation; the trace keeps the full result."""

    import urban_dossier_analyst.agent_loop as loop

    big = {"scores": {"safety": 35}, "evidence_table": "x" * 40000}
    original = loop.dispatch_tool
    loop.dispatch_tool = lambda name, args: big
    try:
        tool_call = [
            {
                "id": "call_1",
                "function": {"name": "score_neighborhood", "arguments": "{}"},
            }
        ]
        client = _StubClient(
            [
                (None, None, "tool_calls", tool_call),
                ("Safety is 35.", None, "stop", None),
            ]
        )

        result = run_agent(user_message="q", client_factory=lambda _u: client)
    finally:
        loop.dispatch_tool = original

    tool_msg = [m for m in client.calls[-1]["messages"] if m.get("role") == "tool"][0]
    assert len(tool_msg["content"]) < 40000
    assert "_omitted_fields" in tool_msg["content"]
    # Full fidelity is preserved for the UI/evidence surface.
    assert result["trace"][0]["result"]["evidence_table"] == "x" * 40000


# --------------------------------------------------------------------------- #
# Portability of the message stream across chat templates
# --------------------------------------------------------------------------- #


def test_only_the_leading_message_is_a_system_message():
    """A system message anywhere but position 0 is a 400 on some models.

    Qwen3.8's chat template answers a later system message with HTTP 400
    "System message must be at the beginning" and takes the whole request
    down.  On 2026-08-14 that killed 3 of its 20 business-eval cases, scoring
    a harness incompatibility as a model failure.  Both mid-conversation
    directives (reflection, final-answer) must therefore go in as role="user".
    """

    import urban_dossier_analyst.agent_loop as loop

    # Distinct args each turn, so the repeated-call guard does not abort the
    # loop before the injections happen.
    script = [
        (
            None,
            None,
            "tool_calls",
            [{
                "id": f"call_{i}",
                "function": {
                    "name": "score_neighborhood",
                    "arguments": json.dumps({"latitude": 40.7 + i / 100}),
                },
            }],
        )
        for i in range(4)
    ]
    script.append(("Done.", None, "stop", None))

    original = loop.dispatch_tool
    loop.dispatch_tool = lambda name, args: {"scores": {"safety": 35}}
    try:
        client = _StubClient(script)
        # reflection_every=1 injects the reflection prompt from iteration 1;
        # max_iterations=4 then forces the final-answer wrap-up as well.
        run_agent(
            user_message="q",
            client_factory=lambda _u: client,
            max_iterations=4,
            reflection_every=1,
        )
    finally:
        loop.dispatch_tool = original

    for call in client.calls:
        roles = [m.get("role") for m in call["messages"]]
        assert roles[0] == "system", roles
        assert "system" not in roles[1:], (
            f"system message at index {roles[1:].index('system') + 1}: {roles}"
        )

    # And the directives really were injected -- otherwise this passes vacuously.
    last = client.calls[-1]["messages"]
    injected = [m for m in last if m.get("role") == "user"
                and str(m.get("content", "")).startswith("[")]
    assert injected, [m.get("role") for m in last]


# --------------------------------------------------------------------------- #
# Sampling profiles
# --------------------------------------------------------------------------- #


def test_sampling_defaults_to_the_production_temperature():
    assert resolve_sampling(None, wrapup=False) == {"temperature": 0.2}


def test_vllm_only_knobs_travel_in_extra_body():
    """top_k is not an OpenAI named argument; passing it as one is a 400."""

    kwargs = resolve_sampling(
        {"temperature": 1.0, "top_p": 0.95, "top_k": 20}, wrapup=False
    )

    assert kwargs["temperature"] == 1.0
    assert kwargs["top_p"] == 0.95
    assert "top_k" not in kwargs
    assert kwargs["extra_body"] == {"top_k": 20}


def test_wrapup_profile_overrides_the_thinking_profile():
    """The wrap-up runs with thinking off, which model cards give its own
    numbers for -- Qwen3.8 asks for 1.0 thinking and 0.7 instruct."""

    profile = {
        "temperature": 1.0,
        "top_p": 0.95,
        "wrapup": {"temperature": 0.7, "top_p": 0.80, "presence_penalty": 1.5},
    }

    loop_kwargs = resolve_sampling(profile, wrapup=False)
    wrap_kwargs = resolve_sampling(profile, wrapup=True)

    assert loop_kwargs["temperature"] == 1.0
    assert "presence_penalty" not in loop_kwargs
    assert wrap_kwargs["temperature"] == 0.7
    assert wrap_kwargs["top_p"] == 0.80
    assert wrap_kwargs["presence_penalty"] == 1.5
    # `wrapup` itself must never reach the API as a sampling parameter.
    assert "wrapup" not in wrap_kwargs
    assert "wrapup" not in (wrap_kwargs.get("extra_body") or {})


def test_wrapup_without_its_own_profile_uses_the_wrapup_temperature():
    kwargs = resolve_sampling({"temperature": 1.0, "top_p": 0.9}, wrapup=True)

    assert kwargs["temperature"] == 0.2
    assert kwargs["top_p"] == 0.9


def test_sampling_profile_reaches_the_completions_call():
    client = _StubClient([("Done.", None, "stop", None)])

    run_agent(
        user_message="q",
        client_factory=lambda _u: client,
        sampling={"temperature": 0.9, "top_k": 40},
    )

    assert client.calls[0]["temperature"] == 0.9
    assert client.calls[0]["extra_body"] == {"top_k": 40}


# --------------------------------------------------------------------------- #
# No-progress guard
# --------------------------------------------------------------------------- #


def _lookup_script(n, name="search_address"):
    """n iterations that each call a lookup tool with a DIFFERENT argument."""

    return [
        (
            None,
            None,
            "tool_calls",
            [{
                "id": f"call_{i}",
                "function": {
                    "name": name,
                    "arguments": json.dumps({"query": f"union square attempt {i}"}),
                },
            }],
        )
        for i in range(n)
    ]


def test_lookup_only_streak_gets_nudged_toward_an_analysis_tool():
    """The failure the identical-argument repeat guard cannot see.

    Qwen3.8 called search_address five times with a different spelling each
    time and never reached a scoring tool. Every hash differed, so the repeat
    guard stayed silent while the whole iteration budget went on geocoding.
    """

    import urban_dossier_analyst.agent_loop as loop

    script = _lookup_script(3)
    script.append(("Done.", None, "stop", None))

    original = loop.dispatch_tool
    loop.dispatch_tool = lambda name, args: {"results": []}
    try:
        client = _StubClient(script)
        run_agent(
            user_message="q",
            client_factory=lambda _u: client,
            max_iterations=6,
            reflection_every=99,  # isolate the no-progress directive
        )
    finally:
        loop.dispatch_tool = original

    directives = [
        m["content"]
        for m in client.calls[-1]["messages"]
        if m.get("role") == "user" and str(m.get("content", "")).startswith("[No progress]")
    ]
    assert directives, "three lookup-only iterations must be called out"
    assert "search_address" in directives[0]


def test_ignoring_the_nudge_forces_an_honest_wrapup():
    import urban_dossier_analyst.agent_loop as loop

    # Nudged at streak 3, cut off at streak 5: five lookup-only iterations,
    # then the forced wrap-up consumes the sixth scripted reply.
    script = _lookup_script(5)
    script.append(("Best I can say is I could not resolve the place.", None,
                   "stop", None))

    original = loop.dispatch_tool
    loop.dispatch_tool = lambda name, args: {"results": []}
    try:
        client = _StubClient(script)
        result = run_agent(
            user_message="q",
            client_factory=lambda _u: client,
            max_iterations=8,
            reflection_every=99,
        )
    finally:
        loop.dispatch_tool = original

    assert result["answer"] == "Best I can say is I could not resolve the place."
    assert [t["kind"] for t in result["turns"]][-1] == "wrapup_no_progress"
    # Stopped early rather than burning the whole budget on geocoding.
    assert result["iterations"] < 8


def test_an_analysis_call_resets_the_streak():
    """A legitimate geocode-then-score run must never trip the guard."""

    import urban_dossier_analyst.agent_loop as loop

    script = _lookup_script(2)
    script.append((
        None, None, "tool_calls",
        [{"id": "call_s", "function": {"name": "score_neighborhood",
                                       "arguments": "{}"}}],
    ))
    script.extend(_lookup_script(2))
    script.append(("Done.", None, "stop", None))

    original = loop.dispatch_tool
    loop.dispatch_tool = lambda name, args: {"scores": {"safety": 35}}
    try:
        client = _StubClient(script)
        run_agent(
            user_message="q",
            client_factory=lambda _u: client,
            max_iterations=8,
            reflection_every=99,
        )
    finally:
        loop.dispatch_tool = original

    nudges = [
        m for call in client.calls for m in call["messages"]
        if str(m.get("content", "")).startswith("[No progress]")
    ]
    assert nudges == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
