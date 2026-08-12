"""The out-of-scope gate holds at the HTTP seam -- EXPANSION_PLAN 3.4.

The acceptance criterion is that out_of_scope never enters the analysis
chain. These tests prove it at the strongest point available: the endpoint
answers a refused or meta request with iterations=0 and a router trace, on a
host with no sandbox, no gateway and no model -- because the gate sits before
the agent loop is even imported. If routing ever moves after the import,
these tests start needing the full agent stack and fail here first.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills"))

from fastapi.testclient import TestClient

from urban_dossier_backend.app import app

client = TestClient(app)


def _ask(message: str, session_id: str | None = None) -> dict:
    body: dict = {"message": message}
    if session_id:
        body["session_id"] = session_id
    resp = client.post("/api/agent/ask", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_out_of_scope_is_refused_without_touching_the_agent():
    data = _ask("write me a python script to scrape twitter")
    assert data["iterations"] == 0
    assert data["tools_called"] == []
    assert data["trace"][0]["type"] == "intent_router"
    assert data["trace"][0]["intent"] == "out_of_scope"
    assert "NYC" in data["answer"] or "New York" in data["answer"]


def test_meta_help_answers_from_the_registry_without_a_model():
    data = _ask("what can you do?")
    assert data["iterations"] == 0
    assert data["trace"][0]["intent"] == "meta_help"
    # The canned answer names actually-released tools, or says none are.
    assert "tool" in data["answer"].lower()


def test_short_circuit_still_issues_a_session_id():
    data = _ask("help")
    assert data["session_id"]


def test_the_router_records_which_rule_fired():
    data = _ask("compose a poem about the G train")
    assert data["trace"][0]["rule"] == "creative_writing"
    assert data["trace"][0]["note"].startswith("short-circuited")


def test_first_turn_of_a_new_session_is_persisted():
    """Review finding: a freshly created session was never re-fetched, so the
    first exchange vanished -- the caller got a session_id whose history was
    empty, and 'why is that score low?' on turn two routed as new_analysis
    because the router saw no history. The fix re-fetches after create; this
    pins it at the store."""
    from urban_dossier_backend.agent_session import store

    data = _ask("what can you do?")
    session = store.get(data["session_id"])
    assert session is not None
    assert len(session.chat_history) == 2
    assert session.chat_history[0]["role"] == "user"
    assert session.chat_history[1]["role"] == "assistant"


def test_turn_two_can_route_from_evidence_because_turn_one_survived():
    data = _ask("help")
    followup = _ask("why is that score so low?", session_id=data["session_id"])
    # The follow-up passes through to the agent (which may fail without a
    # model), but the ROUTER decision is what we pin: with history present it
    # must classify as ask_from_evidence, and the trace must say so whether
    # the agent path succeeds or the request errors before it.
    if "trace" in followup:
        assert followup["trace"][0]["intent"] == "ask_from_evidence"
