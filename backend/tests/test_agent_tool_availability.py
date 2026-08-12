from __future__ import annotations

import json

from fastapi.testclient import TestClient

from urban_dossier_analyst import tools
from urban_dossier_analyst import agent_loop
from urban_dossier_backend.app import app


CORE = {
    "score_neighborhood",
    "compare_neighborhoods",
    "query_dataset",
    "search_address",
}


def _published_names() -> set[str]:
    return {entry["function"]["name"] for entry in tools.get_available_tools()}


def test_missing_artifacts_are_not_published_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("URBAN_DOSSIER_WALK_GRAPH_DIR", str(tmp_path / "walk"))
    monkeypatch.setenv("URBAN_DOSSIER_ELASTICITY_PATH", str(tmp_path / "elasticity.json"))
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "index"))

    states = tools.tool_availability()

    assert _published_names() == CORE
    assert states["find_similar_neighborhoods"]["reason"] == (
        "dedicated_similarity_not_implemented"
    )
    assert states["walking_isochrone"]["reason"] == "walking_graph_missing"
    assert states["simulate_intervention"]["reason"] == "elasticity_artifact_missing"
    assert states["retrieve_dataset_docs"]["reason"] == "rag_index_missing"


def test_valid_artifacts_publish_tools_and_narrow_simulation_enum(tmp_path, monkeypatch):
    walk = tmp_path / "walk"
    walk.mkdir()
    (walk / "walk_nodes.parquet").write_bytes(b"parquet-nodes")
    (walk / "walk_edges.parquet").write_bytes(b"parquet-edges")
    (walk / "walk_graph.manifest.json").write_text(
        json.dumps({"network_type": "walking", "node_count": 3, "edge_count": 2})
    )
    elasticity = tmp_path / "elasticity.json"
    elasticity.write_text(
        json.dumps(
            {
                "interventions": {
                    "park": {"available": True},
                    "toilet": {"available": False},
                }
            }
        )
    )
    index = tmp_path / "index"
    index.mkdir()
    (index / "corpus.faiss").write_bytes(b"not-loaded-by-the-gate")
    (index / "corpus.faiss.meta.json").write_text(
        json.dumps({"dim": 4, "metadata": [{"dataset_id": "parks"}]})
    )
    monkeypatch.setenv("URBAN_DOSSIER_WALK_GRAPH_DIR", str(walk))
    monkeypatch.setenv("URBAN_DOSSIER_ELASTICITY_PATH", str(elasticity))
    monkeypatch.setenv("RAG_INDEX_DIR", str(index))

    published = tools.get_available_tools()
    names = {entry["function"]["name"] for entry in published}

    assert names == CORE | {
        "walking_isochrone",
        "simulate_intervention",
        "retrieve_dataset_docs",
    }
    simulation = next(
        entry for entry in published if entry["function"]["name"] == "simulate_intervention"
    )
    assert simulation["function"]["parameters"]["properties"]["intervention_type"][
        "enum"
    ] == ["park"]
    original = next(
        entry for entry in tools.TOOLS if entry["function"]["name"] == "simulate_intervention"
    )
    assert "toilet" in original["function"]["parameters"]["properties"]["intervention_type"][
        "enum"
    ]


def test_status_exposes_decisions_without_local_artifact_paths(tmp_path, monkeypatch):
    secret_path = tmp_path / "operator-secret" / "walk"
    monkeypatch.setenv("URBAN_DOSSIER_WALK_GRAPH_DIR", str(secret_path))
    monkeypatch.setenv("URBAN_DOSSIER_ELASTICITY_PATH", str(tmp_path / "private.json"))
    monkeypatch.setenv("RAG_INDEX_DIR", str(tmp_path / "private-index"))

    response = TestClient(app).get("/api/agent/status")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tools"]) == {entry["function"]["name"] for entry in tools.TOOLS}
    assert set(payload["available_tools"]) == CORE
    assert "find_similar_neighborhoods" in payload["unavailable_tools"]
    assert str(tmp_path) not in response.text


def test_agent_refuses_forged_call_to_unreleased_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("URBAN_DOSSIER_WALK_GRAPH_DIR", str(tmp_path / "missing"))

    class Message:
        def __init__(self, content, tool_calls):
            self.content = content
            self.tool_calls = tool_calls

        def model_dump(self):
            return {"content": self.content, "tool_calls": self.tool_calls}

    class Completions:
        calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                message = Message(
                    None,
                    [
                        {
                            "id": "forged",
                            "function": {
                                "name": "walking_isochrone",
                                "arguments": '{"latitude":40.75,"longitude":-73.99}',
                            },
                        }
                    ],
                )
                choice = type("Choice", (), {"message": message, "finish_reason": "tool_calls"})
            else:
                message = Message("Walking routing is not enabled.", [])
                choice = type("Choice", (), {"message": message, "finish_reason": "stop"})
            return type("Response", (), {"choices": [choice]})

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    monkeypatch.setattr(
        agent_loop,
        "dispatch_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    result = agent_loop.run_agent("walk from here", client_factory=lambda _url: client)

    assert result["answer"] == "Walking routing is not enabled."
    assert result["trace"][0]["result"]["error"] == "tool_not_released"
    assert result["trace"][0]["result"]["reason"] == "walking_graph_missing"
