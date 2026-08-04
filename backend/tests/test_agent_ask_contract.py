"""Contract tests for POST /api/agent/ask.

These lock the seam between the FastAPI handler and the out-of-tree skill at
``skills/urban_dossier_analyst``. The endpoint previously returned HTTP 500 on
every call because the handler invoked ``run_agent(message=..., session_id=...)``
while the skill exposes ``run_agent(user_message=...)`` and takes no
``session_id``. Nothing failed at import time, so the break only surfaced at
request time.

The signature test below binds the real call keywords against the real skill
function, so the same drift fails in CI instead of in production.
"""

from __future__ import annotations

import inspect

from urban_dossier_analyst.agent_loop import run_agent

from urban_dossier_backend.app import AskResponse, _normalize_tools_called


def test_handler_call_keywords_bind_against_the_real_run_agent():
    """The exact keywords the handler passes must bind to the skill signature."""

    inspect.signature(run_agent).bind(
        user_message="how safe is the East Village?",
        history=[],
        max_iterations=8,
        vllm_base_url="http://127.0.0.1:8000/v1",
        model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4",
    )


def test_run_agent_does_not_accept_session_id():
    """Session ownership stays in FastAPI; the skill must remain stateless.

    If someone later adds ``session_id`` to the skill, this test fails and
    forces a deliberate decision about who owns session state.
    """

    assert "session_id" not in inspect.signature(run_agent).parameters


def test_skill_tools_called_shape_survives_the_response_model():
    """The skill emits bare tool names; AskResponse publishes objects.

    Without the adapter this raised ResponseValidationError -- but only once the
    model actually called a tool, so an empty-tool smoke test would pass while
    every real analytical question 500'd.
    """

    from_skill = ["score_neighborhood", "query_dataset"]

    response = AskResponse(
        answer="a",
        evidence=[],
        tools_called=_normalize_tools_called(from_skill),
        iterations=2,
        trace=[],
        session_id="s",
    )

    assert response.tools_called == [
        {"name": "score_neighborhood"},
        {"name": "query_dataset"},
    ]


def test_normalize_tools_called_passes_dicts_through_and_tolerates_empty():
    assert _normalize_tools_called(None) == []
    assert _normalize_tools_called([]) == []
    assert _normalize_tools_called([{"name": "query_dataset", "latency_ms": 12}]) == [
        {"name": "query_dataset", "latency_ms": 12}
    ]


def test_run_agent_returns_every_field_the_response_model_requires():
    """Guard the return contract, not just the call contract."""

    class _Message:
        content = "final answer"
        tool_calls = None

        def model_dump(self) -> dict:
            return {"content": self.content, "tool_calls": None}

    class _Client:
        class chat:  # noqa: N801 - mirrors the OpenAI SDK attribute layout
            class completions:
                @staticmethod
                def create(**_kwargs):
                    choice = type("Choice", (), {"message": _Message()})
                    return type("Response", (), {"choices": [choice]})

    result = run_agent(user_message="hi", client_factory=lambda _url: _Client())

    for field in ("answer", "evidence", "tools_called", "iterations", "trace"):
        assert field in result, f"run_agent stopped returning {field!r}"

    AskResponse(
        answer=result["answer"],
        evidence=result["evidence"],
        tools_called=_normalize_tools_called(result["tools_called"]),
        iterations=result["iterations"],
        trace=result["trace"],
        session_id="s",
    )
