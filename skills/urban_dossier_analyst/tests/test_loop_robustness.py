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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
