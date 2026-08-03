from types import SimpleNamespace

from urban_dossier_backend import agent_service
from urban_dossier_backend.agent_service import (
    _decode_nemoclaw_payload,
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
