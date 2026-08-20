"""Mac-runnable smoke tests for the urban-dossier-analyst skill.

Run with:
  cd Urban-Dossier/skills/urban-dossier-analyst
  python -m unittest tests.test_smoke -v

These tests do NOT require a running vLLM server or FastAPI backend. The
agent_loop test injects a stub OpenAI client via the client_factory seam.
The dispatch_tool tests exercise NotImplementedError / validation branches
which do not hit the network.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any


# The skill directory uses a hyphen, so it cannot be imported via dotted
# Python syntax. We side-load each submodule via importlib.util under a
# synthetic package name that the relative imports inside the skill modules
# can resolve against.
_SKILL_DIR = Path(__file__).resolve().parents[1]
_PKG_NAME = "urban_dossier_analyst_pkg"


def _load_pkg() -> Any:
    """Register a synthetic package so `from .schemas import ...` resolves."""

    if _PKG_NAME in sys.modules:
        return sys.modules[_PKG_NAME]
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _SKILL_DIR / "__init__.py",
        submodule_search_locations=[str(_SKILL_DIR)],
    )
    assert spec is not None and spec.loader is not None
    pkg = importlib.util.module_from_spec(spec)
    # Do not exec the package __init__ - it imports openai eagerly. Just
    # register the bare package so submodule loads work.
    sys.modules[_PKG_NAME] = pkg
    return pkg


def _load_submodule(name: str) -> Any:
    """Load a submodule under the synthetic package name."""

    full = f"{_PKG_NAME}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _SKILL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load_pkg()
_load_submodule("schemas")
_load_submodule("prompts")
tools = _load_submodule("tools")
agent_loop = _load_submodule("agent_loop")


# --------------------------------------------------------------------------- #
# 1. TOOLS list shape
# --------------------------------------------------------------------------- #


class ToolsListShapeTest(unittest.TestCase):
    def test_exactly_seven_tools(self) -> None:
        # 8 -> 7 when retrieve_dataset_docs went with the RAG subsystem
        # (2026-08-20). The count is pinned so a tool cannot appear or vanish
        # without a deliberate edit here.
        self.assertEqual(len(tools.TOOLS), 7)

    def test_each_tool_has_required_keys(self) -> None:
        expected_names = {
            "score_neighborhood",
            "compare_neighborhoods",
            "query_dataset",
            "find_similar_neighborhoods",
            "walking_isochrone",
            "simulate_intervention",
            "search_address",
        }
        seen_names: set[str] = set()
        for entry in tools.TOOLS:
            self.assertEqual(entry["type"], "function")
            fn = entry["function"]
            for key in ("name", "description", "parameters"):
                self.assertIn(key, fn, f"missing {key} in tool {fn.get('name')}")
            self.assertEqual(fn["parameters"]["type"], "object")
            self.assertIn("properties", fn["parameters"])
            seen_names.add(fn["name"])
        self.assertEqual(seen_names, expected_names)


# --------------------------------------------------------------------------- #
# 2. Pydantic argument models accept sample input
# --------------------------------------------------------------------------- #


class PydanticArgModelsTest(unittest.TestCase):
    def test_score_neighborhood_args(self) -> None:
        m = tools.ScoreNeighborhoodArgs(latitude=40.75, longitude=-73.99)
        self.assertEqual(m.radius_m, 500)

    def test_compare_neighborhoods_args(self) -> None:
        m = tools.CompareNeighborhoodsArgs(
            point_a={"latitude": 40.75, "longitude": -73.99},
            point_b={"latitude": 40.71, "longitude": -73.95},
        )
        self.assertEqual(m.radius_m, 500)

    def test_query_dataset_args(self) -> None:
        m = tools.QueryDatasetArgs(dataset_id="safety", filters={}, limit=50)
        self.assertEqual(m.limit, 50)

    def test_find_similar_args(self) -> None:
        m = tools.FindSimilarNeighborhoodsArgs(latitude=40.75, longitude=-73.99, k=3)
        self.assertEqual(m.k, 3)

    def test_walking_isochrone_args(self) -> None:
        m = tools.WalkingIsochroneArgs(latitude=40.75, longitude=-73.99, minutes=15)
        self.assertEqual(m.minutes, 15)

    def test_simulate_intervention_args(self) -> None:
        m = tools.SimulateInterventionArgs(
            latitude=40.75,
            longitude=-73.99,
            intervention_type="bike_lane",
            count=2,
        )
        self.assertEqual(m.count, 2)

    def test_search_address_args(self) -> None:
        m = tools.SearchAddressArgs(query="Empire State", limit=3)
        self.assertEqual(m.limit, 3)


# --------------------------------------------------------------------------- #
# 3. dispatch_tool always returns a dict (never raises) on failure
# --------------------------------------------------------------------------- #


class DispatchToolErrorHandlingTest(unittest.TestCase):
    def test_unknown_tool_returns_error_dict(self) -> None:
        out = tools.dispatch_tool("does_not_exist", {})
        self.assertIn("error", out)
        self.assertIn("retry_hint", out)

    def test_validation_error_returns_dict(self) -> None:
        # Missing required latitude / longitude.
        out = tools.dispatch_tool("score_neighborhood", {})
        self.assertIn("error", out)
        self.assertIn("validation", out["error"].lower())

    def test_not_implemented_returns_dict_with_endpoint_hint(self) -> None:
        """dispatch_tool must convert NotImplementedError into an observation.

        This used to probe walking_isochrone, which was a stub. It now does
        real street-network routing, so the contract is exercised against an
        injected raiser instead -- the guarantee under test is that
        dispatch_tool never propagates the exception, not that any particular
        tool is still unimplemented.
        """

        arg_model, original = tools._TOOL_REGISTRY["walking_isochrone"]

        def _raise(_args):
            raise NotImplementedError(
                "Tool walking_isochrone requires backend endpoint POST /api/isochrone."
            )

        tools._TOOL_REGISTRY["walking_isochrone"] = (arg_model, _raise)
        try:
            out = tools.dispatch_tool(
                "walking_isochrone",
                {"latitude": 40.75, "longitude": -73.99, "minutes": 10},
            )
        finally:
            tools._TOOL_REGISTRY["walking_isochrone"] = (arg_model, original)

        self.assertIn("error", out)
        self.assertIn("/api/isochrone", out["error"])
        self.assertIn("retry_hint", out)

    def test_walking_isochrone_is_implemented(self) -> None:
        """Guard against the tool silently regressing to a stub."""

        _arg_model, impl = tools._TOOL_REGISTRY["walking_isochrone"]
        import inspect

        self.assertNotIn("raise NotImplementedError", inspect.getsource(impl))


# --------------------------------------------------------------------------- #
# 4. agent_loop terminates when the stub LLM returns no tool calls
# --------------------------------------------------------------------------- #


class _StubMsg:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None

    def model_dump(self) -> dict[str, Any]:
        return {"role": "assistant", "content": self.content, "tool_calls": []}


class _StubChoice:
    def __init__(self, content: str) -> None:
        self.message = _StubMsg(content)


class _StubResp:
    def __init__(self, content: str) -> None:
        self.choices = [_StubChoice(content)]


class _StubChatCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **kwargs: Any) -> _StubResp:
        return _StubResp(self._content)


class _StubChat:
    def __init__(self, content: str) -> None:
        self.completions = _StubChatCompletions(content)


class _StubClient:
    def __init__(self, content: str) -> None:
        self.chat = _StubChat(content)


class AgentLoopTerminationTest(unittest.TestCase):
    def test_terminates_immediately_on_text_only_response(self) -> None:
        def factory(_url: str) -> _StubClient:
            return _StubClient("Astoria has solid transit and many parks.")

        out = agent_loop.run_agent(
            user_message="Tell me about Astoria",
            client_factory=factory,
            max_iterations=5,
        )
        self.assertEqual(out["iterations"], 1)
        self.assertIn("Astoria", out["answer"])
        self.assertEqual(out["tools_called"], [])
        self.assertEqual(out["trace"], [])
        self.assertEqual(out["evidence"], [])


if __name__ == "__main__":
    unittest.main()
