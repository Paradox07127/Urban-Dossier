"""Tests for the OpenClaw Gateway transport.

These lock in the two Gateway constraints that dictate the adapter's design,
both verified against the running 2026.7.1 Gateway:

  1. ``input`` must be a plain string. The array form carrying
     ``function_call_output`` items is rejected outright, so tool results go
     back as text.
  2. Conversation state is server-side, keyed by ``x-openclaw-session-key``.
     Each turn must therefore send only what is new -- replaying the whole
     history would duplicate it in the agent's context.

No network: the adapter takes an injectable ``http_post``.
"""

from __future__ import annotations

import json

from urban_dossier_analyst.agent_loop import run_agent
from urban_dossier_analyst.gateway import (
    GatewayChatAdapter,
    _chat_tools_to_responses,
    _parse_response,
    _render_turn,
)


CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "score_neighborhood",
        "description": "Score a point",
        "parameters": {"type": "object", "properties": {"latitude": {"type": "number"}}},
    },
}


# --------------------------------------------------------------------------- #
# Schema translation
# --------------------------------------------------------------------------- #


def test_chat_tools_are_flattened_to_the_responses_shape():
    converted = _chat_tools_to_responses([CHAT_TOOL])

    assert converted == [
        {
            "type": "function",
            "name": "score_neighborhood",
            "description": "Score a point",
            "parameters": {"type": "object", "properties": {"latitude": {"type": "number"}}},
        }
    ]
    # The nested "function" wrapper must be gone -- the Gateway rejects it.
    assert "function" not in converted[0]


def test_function_call_output_maps_back_to_chat_tool_calls():
    body = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "call_abc",
                "call_id": "8de107ece",
                "name": "score_neighborhood",
                "arguments": '{"latitude":40.7}',
            }
        ],
    }

    choice = _parse_response(body)

    assert choice.finish_reason == "tool_calls"
    call = choice.message.tool_calls[0]
    # call_id, not id: call_id is what the Gateway correlates on.
    assert call["id"] == "8de107ece"
    assert call["function"]["name"] == "score_neighborhood"
    assert json.loads(call["function"]["arguments"]) == {"latitude": 40.7}


def test_message_output_maps_to_content():
    body = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "safety is 35"}]}],
    }

    choice = _parse_response(body)

    assert choice.message.content == "safety is 35"
    assert choice.message.tool_calls is None
    assert choice.finish_reason == "stop"


def test_incomplete_status_maps_to_length_so_the_loop_asks_for_a_wrapup():
    body = {"status": "incomplete", "output": []}

    assert _parse_response(body).finish_reason == "length"


# --------------------------------------------------------------------------- #
# Turn rendering
# --------------------------------------------------------------------------- #


def test_tool_results_are_rendered_as_text_not_function_call_output():
    rendered = _render_turn(
        [{"role": "tool", "name": "score_neighborhood", "content": '{"safety":35}'}]
    )

    assert "[Tool result: score_neighborhood]" in rendered
    assert '{"safety":35}' in rendered


def test_assistant_messages_are_not_echoed_back():
    """The Gateway already holds its own output; resending duplicates it."""

    rendered = _render_turn(
        [
            {"role": "assistant", "content": "I will call a tool", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "name": "t", "content": "{}"},
        ]
    )

    assert "I will call a tool" not in rendered


# --------------------------------------------------------------------------- #
# Statefulness
# --------------------------------------------------------------------------- #


def _recording_adapter(script):
    """Adapter whose http_post replays `script` and records every payload."""

    sent: list[dict] = []

    def _post(payload):
        sent.append(payload)
        return script[len(sent) - 1]

    adapter = GatewayChatAdapter(token="test-token", http_post=_post, session_key="sk-test")
    return adapter, sent


def test_each_turn_sends_only_new_messages():
    """Server-side session state means history must not be replayed."""

    script = [
        {"status": "completed", "output": [{"type": "message", "content": [{"text": "hi"}]}]},
        {"status": "completed", "output": [{"type": "message", "content": [{"text": "bye"}]}]},
    ]
    adapter, sent = _recording_adapter(script)

    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "first"}]
    adapter.create(messages=messages)

    messages = messages + [
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "second"},
    ]
    adapter.create(messages=messages)

    assert "first" in sent[0]["input"]
    # Turn two must not repeat turn one.
    assert "first" not in sent[1]["input"]
    assert "second" in sent[1]["input"]


def test_input_is_always_a_string():
    """Array input is rejected by the Gateway with 'input: Invalid input'."""

    script = [{"status": "completed", "output": [{"type": "message", "content": [{"text": "ok"}]}]}]
    adapter, sent = _recording_adapter(script)

    adapter.create(messages=[{"role": "user", "content": "q"}], tools=[CHAT_TOOL])

    assert isinstance(sent[0]["input"], str)


def test_wrapup_call_does_not_offer_tools():
    """tool_choice='none' is the loop asking for prose; sending tools invites another call."""

    script = [{"status": "completed", "output": [{"type": "message", "content": [{"text": "final"}]}]}]
    adapter, sent = _recording_adapter(script)

    adapter.create(messages=[{"role": "user", "content": "q"}], tools=[CHAT_TOOL], tool_choice="none")

    assert "tools" not in sent[0]


def test_empty_delta_does_not_send_empty_input():
    """The Gateway rejects an empty input; send a nudge instead."""

    script = [{"status": "completed", "output": [{"type": "message", "content": [{"text": "ok"}]}]}]
    adapter, sent = _recording_adapter(script)

    adapter.create(messages=[{"role": "assistant", "content": "only an echo"}])

    assert sent[0]["input"].strip()


# --------------------------------------------------------------------------- #
# End-to-end through the loop
# --------------------------------------------------------------------------- #


def test_run_agent_drives_a_tool_call_over_the_gateway_transport():
    import urban_dossier_analyst.agent_loop as loop

    script = [
        {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "score_neighborhood",
                    "arguments": '{"latitude":40.7265,"longitude":-73.9815}',
                }
            ],
        },
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"text": "Safety is 35."}]}],
        },
    ]
    sent: list[dict] = []

    def _post(payload):
        sent.append(payload)
        return script[len(sent) - 1]

    original = loop.dispatch_tool
    loop.dispatch_tool = lambda name, args: {"scores": {"safety": 35}}
    try:
        result = run_agent(
            user_message="how safe?",
            client_factory=lambda _u: GatewayChatAdapter(token="t", http_post=_post),
        )
    finally:
        loop.dispatch_tool = original

    assert result["answer"] == "Safety is 35."
    assert result["tools_called"] == ["score_neighborhood"]
    # Turn 2 carries the tool result as text.
    assert "[Tool result: score_neighborhood]" in sent[1]["input"]
