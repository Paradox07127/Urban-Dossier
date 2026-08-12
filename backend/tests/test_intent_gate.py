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
