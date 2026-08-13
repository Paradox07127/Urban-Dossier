"""The business eval set (EXPANSION_PLAN 4.1) — case file and graders.

The eval set is the ground every model decision stands on, so its two
halves are pinned separately: the case FILE must stay well-formed (unique
ids, known categories, compilable regexes, spec'd expect keys), and every
GRADER must be provably right on synthetic responses — no model, no
backend, no network.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "vllm"))

from business_eval import grade_case, run_routing_case  # noqa: E402

CASES = json.loads(
    (REPO_ROOT / "evals" / "agent" / "model_cases.json").read_text(encoding="utf-8")
)

KNOWN_CATEGORIES = {
    "routing", "tool_call", "evidence", "multi_step", "format", "robustness",
}
KNOWN_EXPECT_KEYS = {
    "route_intent", "route_rule", "tools_all", "tools_any", "tools_forbidden",
    "order", "order_mode", "args_contain", "min_tool_calls",
    "answer_regex_all", "answer_regex_any", "answer_forbidden_regex",
    "citation_required", "evidence_list_required", "json_answer_keys",
    "max_sentences", "either", "no_numbers_without_tools",
    "numeric_faithfulness",
}


# --- the case file ----------------------------------------------------------


def test_case_file_shape():
    ids = [case["id"] for case in CASES["cases"]]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    assert 20 <= len(ids) <= 30, "plan 4.1 calls for 20-30 cases"
    for case in CASES["cases"]:
        assert case["category"] in KNOWN_CATEGORIES, case["id"]
        assert case["prompt"].strip()
        unknown = set(case["expect"]) - KNOWN_EXPECT_KEYS
        assert not unknown, f"{case['id']}: unknown expect keys {unknown}"


def test_all_regexes_compile():
    for case in CASES["cases"]:
        expect = case["expect"]
        for key in ("answer_regex_all", "answer_regex_any", "answer_forbidden_regex"):
            for pattern in expect.get(key, []):
                re.compile(pattern, re.IGNORECASE)
        for pattern in (expect.get("either") or {}).get("answer_regex_any", []):
            re.compile(pattern, re.IGNORECASE)


def test_routing_cases_pass_against_live_router():
    """The routing cases are pinned to the actual router, not to hope."""
    for case in CASES["cases"]:
        if case["category"] == "routing":
            result = run_routing_case(case)
            assert result["status"] == "pass", (case["id"], result["failures"])


# --- graders on synthetic responses -----------------------------------------


def _response(**overrides):
    base = {
        "answer": "Overall score is 62 [score_neighborhood via analyze-point].",
        "evidence": [{"source": "score_neighborhood", "detail": "overall 62"}],
        "tools_called": ["score_neighborhood"],
        "iterations": 2,
        "trace": [
            {
                "iteration": 0,
                "tool_name": "score_neighborhood",
                "args": {"latitude": 40.7282, "longitude": -73.9942},
                "result": {"overall": 62},
                "latency_ms": 90,
            }
        ],
    }
    base.update(overrides)
    return base


def test_tools_all_and_args_tolerance():
    case = {
        "prompt": "score 40.7282, -73.9942",
        "expect": {
            "tools_all": ["score_neighborhood"],
            "args_contain": {
                "score_neighborhood": {"latitude": 40.7282, "longitude": -73.9942}
            },
        },
    }
    assert grade_case(case, _response())["status"] == "pass"
    # 0.01 float tolerance holds; 0.05 off fails.
    near = _response()
    near["trace"][0]["args"] = {"latitude": 40.7284, "longitude": -73.9942}
    assert grade_case(case, near)["status"] == "pass"
    far = _response()
    far["trace"][0]["args"] = {"latitude": 40.78, "longitude": -73.9942}
    assert grade_case(case, far)["status"] == "fail"


def test_order_any_present_only_binds_used_tools():
    case = {
        "prompt": "p",
        "expect": {
            "order": [["search_address", "score_neighborhood"],
                      ["search_address", "query_dataset"]],
            "order_mode": "any_present",
        },
    }
    ok = _response(tools_called=["search_address", "score_neighborhood"])
    assert grade_case(case, ok)["status"] == "pass"
    bad = _response(tools_called=["score_neighborhood", "search_address"])
    assert grade_case(case, bad)["status"] == "fail"


def test_citation_accepts_either_surface():
    case = {"prompt": "p", "expect": {"citation_required": True}}
    inline_only = _response(evidence=[])
    assert grade_case(case, inline_only)["status"] == "pass"
    evidence_only = _response(answer="Overall score is 62, per the score tool.")
    assert grade_case(case, evidence_only)["status"] == "pass"
    neither = _response(answer="Overall score is 62.", evidence=[])
    assert grade_case(case, neither)["status"] == "fail"


def test_forbidden_and_required_patterns():
    case = {
        "prompt": "p",
        "expect": {
            "answer_regex_any": ["(no|not).{0,40}data"],
            "answer_forbidden_regex": ["\\$\\s?\\d{3,}"],
        },
    }
    refusal = _response(answer="I do not have rent data for this block.")
    assert grade_case(case, refusal)["status"] == "pass"
    invented = _response(answer="Median rent is $3450, no data issues.")
    assert grade_case(case, invented)["status"] == "fail"


def test_numeric_faithfulness_is_soft_warn():
    case = {"prompt": "p", "expect": {"numeric_faithfulness": True}}
    grounded = _response()
    assert grade_case(case, grounded)["status"] == "pass"
    invented = _response(
        answer="Overall score is 62 and there were 847 incidents [cited]."
    )
    graded = grade_case(case, invented)
    assert graded["status"] == "warn"
    assert graded["soft"]["faithfulness"]["unsupported"] == ["847"]


def test_json_answer_grading_handles_fences():
    case = {"prompt": "p", "expect": {"json_answer_keys": ["headline", "caveat"]}}
    fenced = _response(
        answer='```json\n{"headline": "h", "evidence": [], "caveat": "c"}\n```'
    )
    assert grade_case(case, fenced)["status"] == "pass"
    missing = _response(answer='{"headline": "h"}')
    assert grade_case(case, missing)["status"] == "fail"


def test_sentence_cap_ignores_decimals():
    case = {"prompt": "p", "expect": {"max_sentences": 3}}
    three = _response(
        answer="Transit is strong at 40.7282. Amenities lag. On balance, move."
    )
    assert grade_case(case, three)["status"] == "pass"
    five = _response(answer="One. Two. Three. Four. Five.")
    assert grade_case(case, five)["status"] == "fail"


def test_no_numbers_without_tools():
    case = {"prompt": "How is Main Street?",
            "expect": {"no_numbers_without_tools": True}}
    honest = _response(
        tools_called=[], trace=[], evidence=[],
        answer="Which Main Street do you mean? Several boroughs have one.",
    )
    assert grade_case(case, honest)["status"] == "pass"
    invented = _response(
        tools_called=[], trace=[], evidence=[],
        answer="Main Street scores 78 overall.",
    )
    assert grade_case(case, invented)["status"] == "fail"


def test_either_branch_passes_on_regex_alone():
    case = {
        "prompt": "p",
        "expect": {
            "either": {
                "tools_any": ["search_address"],
                "answer_regex_any": ["which|clarif"],
            }
        },
    }
    clarifies = _response(tools_called=[], trace=[],
                          answer="Which borough's Main Street do you mean?")
    assert grade_case(case, clarifies)["status"] == "pass"
    neither = _response(tools_called=[], trace=[], answer="It is fine.")
    assert grade_case(case, neither)["status"] == "fail"


def test_canon_normalizes_typographic_punctuation():
    """Regression: a perfect Nano refusal failed grading on U+2019.

    The answer text below is the verbatim failing fixture from the
    2026-08-13 baseline run (curly apostrophe in "isn't").
    """
    case = {
        "prompt": "Query the dataset called 'crime_forecast_2027'.",
        "expect": {
            "answer_regex_any": [
                "(no|not|n't|doesn't|does not|unknown|unavailable|missing|"
                "cannot|can't).{0,80}(dataset|exist|available|found|such)",
            ]
        },
    }
    refusal = _response(
        tools_called=[], trace=[], evidence=[],
        answer=(
            "I’m sorry, but the dataset **`crime_forecast_2027`** "
            "isn’t part of the NYC Open Data sources that are "
            "currently available through the tools I can access."
        ),
    )
    assert grade_case(case, refusal)["status"] == "pass"


def test_tool_args_reject_unknown_arguments():
    """Regression: extras used to be dropped silently (query_dataset given
    latitude/longitude/radius_m returned unfiltered rows the model then
    presented as spatially filtered evidence)."""
    from urban_dossier_analyst.tools import dispatch_tool

    result = dispatch_tool(
        "query_dataset",
        {"dataset_id": "collisions", "latitude": 40.7282,
         "longitude": -73.9942, "radius_m": 500},
    )
    assert "error" in result
    assert "retry_hint" in result
