from types import SimpleNamespace

from urban_dossier_backend import agent_service
from urban_dossier_backend.agent_service import (
    _decode_nemoclaw_payload,
    _md_to_html,
    _openclaw_gateway_agent,
    _response_output_text,
)


def test_decode_nemoclaw_0100_output_with_status_prefix():
    stdout = """✓ Active gateway set to 'nemoclaw'
{"status":"ok","result":{"payloads":[{"text":"nemoclaw-link-ok","mediaUrl":null}]}}
"""

    assert _decode_nemoclaw_payload(stdout) == "nemoclaw-link-ok"


def test_decode_legacy_flat_payloads():
    stdout = '{"payloads":[{"text":"legacy-ok"}]}'

    assert _decode_nemoclaw_payload(stdout) == "legacy-ok"


def test_decode_returns_none_for_non_agent_json():
    assert _decode_nemoclaw_payload('notice {"status":"ok"}') is None


def test_response_output_text_prefers_sdk_convenience_property():
    response = SimpleNamespace(output_text=" dedicated response ", output=[])

    assert _response_output_text(response) == "dedicated response"


def test_markdown_renderer_escapes_raw_html_but_keeps_supported_markup():
    rendered = _md_to_html("## Safe <script>alert(1)</script>\n- **measured**")

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<strong>measured</strong>" in rendered


def test_refined_report_escapes_feedback_and_model_html(monkeypatch):
    malicious = '<img src=x onerror="alert(1)">'
    monkeypatch.setattr(agent_service, "AGENT_BACKEND", "nemoclaw")
    monkeypatch.setattr(
        agent_service,
        "_openclaw_agent",
        lambda *_args, **_kwargs: "## Summary\n" + malicious + " grounded prose " * 5,
    )
    session = SimpleNamespace(analysis_payload={}, generated_reports=[])

    result = agent_service.refine_report(session, malicious)

    assert malicious not in result["html"]
    assert "&lt;img" in result["html"]


def test_generate_report_fails_closed_when_openclaw_fails(monkeypatch):
    monkeypatch.setattr(agent_service, "AGENT_BACKEND", "nemoclaw")
    monkeypatch.setattr(agent_service, "_try_nemoclaw_report", lambda *_args: None)

    result = agent_service.generate_report({})

    assert result == {
        "error": "OpenClaw report generation failed",
        "error_code": "openclaw_unavailable",
        "backend": "nemoclaw",
    }


def test_every_generated_artifact_declares_its_grounding(monkeypatch):
    """The audit's release blocker was a claim with no field behind it.

    SymGen promised deterministic numeric verification and never ran, and
    nothing in the payload said so -- a caller could not tell a verified
    number from an unverified one. Removing SymGen only closes that if the
    artifacts now state it, so every success path carries `grounding`.
    """
    monkeypatch.setattr(agent_service, "AGENT_BACKEND", "nemoclaw")
    monkeypatch.setattr(
        agent_service,
        "_openclaw_agent",
        lambda *_args, **_kwargs: "## Safety\n" + "grounded prose " * 12,
    )

    report = agent_service.generate_report({})
    assert report["grounding"] == agent_service.GROUNDING_NONE
    assert report["grounding"]["verified"] is False
    assert "not independently" in report["html"]

    session = SimpleNamespace(analysis_payload={}, generated_reports=[])
    refined = agent_service.refine_report(session, "focus on safety")
    assert refined["grounding"] == agent_service.GROUNDING_NONE
    assert "not independently" in refined["html"]


def test_module_has_no_direct_model_client():
    """The sandbox boundary is structural, not conditional.

    These tests used to monkeypatch `_get_openai_client` to raise, proving the
    direct path did not run on that call. The scripts mode and its host-vLLM
    client were removed on 2026-08-22, so the stronger statement is available:
    there is no client here to reach past OpenClaw with.
    """
    for name in (
        "_get_openai_client",
        "_llm_chat",
        "_llm_chat_multi",
        "_fallback_script_report",
        "_apply_symgen_pipeline",
        "resolve_symgen",
    ):
        assert not hasattr(agent_service, name), (
            f"{name} is back; report/poster generation must stay inside the "
            "OpenClaw sandbox"
        )


def test_generate_poster_fails_closed_when_openclaw_fails(monkeypatch):
    monkeypatch.setattr(agent_service, "AGENT_BACKEND", "nemoclaw")
    monkeypatch.setattr(agent_service, "_run_script", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(agent_service, "_try_nemoclaw_poster", lambda *_args: None)
    result = agent_service.generate_poster({})

    assert result == {
        "error": "OpenClaw poster generation failed",
        "error_code": "openclaw_unavailable",
        "backend": "nemoclaw",
    }


def test_refine_report_fails_closed_when_openclaw_fails(monkeypatch):
    monkeypatch.setattr(agent_service, "AGENT_BACKEND", "nemoclaw")
    monkeypatch.setattr(agent_service, "_openclaw_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        agent_service,
        "_run_script",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scripts ran")),
    )
    session = SimpleNamespace(analysis_payload={}, generated_reports=[])

    result = agent_service.refine_report(session, "focus on safety")

    assert result == {
        "error": "OpenClaw report refinement failed",
        "error_code": "openclaw_unavailable",
        "backend": "nemoclaw",
    }


def test_gateway_targets_dedicated_agent_and_stable_session(monkeypatch):
    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text="gateway-ok", output=[])

    fake_client = SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(
        agent_service,
        "_get_openclaw_gateway_client",
        lambda refresh=False: fake_client,
    )
    monkeypatch.setattr(agent_service, "OPENCLAW_AGENT_ID", "urban-dossier")

    result = _openclaw_gateway_agent("hello", "chat-123")

    assert result == "gateway-ok"
    assert captured["model"] == "openclaw/urban-dossier"
    assert captured["input"] == "hello"
    assert captured["extra_headers"] == {
        "x-openclaw-agent-id": "urban-dossier",
        "x-openclaw-session-key": "chat-123",
    }


def test_gateway_output_budget_comes_from_the_module_constant(monkeypatch):
    """The Gateway turn must not re-introduce a hardcoded 4096 output cap.

    Nemotron Nano spends its output budget reasoning before it writes any of
    the answer, so a final turn over tool results hit stopReason=length with
    the answer never started and the user saw "Agent couldn't generate a
    response" (2026-08-20). The budget is a tunable constant now; this pins
    that the call reads it rather than a literal, and that the default is
    above the value that demonstrably failed.
    """
    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text="gateway-ok", output=[])

    fake_client = SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(
        agent_service,
        "_get_openclaw_gateway_client",
        lambda refresh=False: fake_client,
    )
    monkeypatch.setattr(agent_service, "OPENCLAW_MAX_OUTPUT_TOKENS", 12345)

    _openclaw_gateway_agent("hello", "chat-123")

    assert captured["max_output_tokens"] == 12345
    assert agent_service.OPENCLAW_MAX_OUTPUT_TOKENS > 4096
