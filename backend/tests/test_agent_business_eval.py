from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "evaluate_agent_business.py"
SPEC = importlib.util.spec_from_file_location("evaluate_agent_business", SCRIPT)
assert SPEC and SPEC.loader
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def test_fixed_corpus_has_24_cases_and_all_business_intents():
    corpus = evaluation.load_corpus(ROOT / "evals" / "agent" / "business_cases.json")

    assert len(corpus["cases"]) == 24
    assert {case["intent"] for case in corpus["cases"]} == evaluation.VALID_INTENTS


def test_grade_case_accepts_required_order_and_evidence():
    case = {
        "id": "named_compare",
        "intent": "new_analysis",
        "ordered_tools": ["search_address", "search_address", "compare_neighborhoods"],
        "forbidden_tools": ["query_dataset"],
        "evidence_required": True,
        "answer_terms_any": ["safer", "comparison"],
    }
    response = {
        "answer": "The comparison is supported by both score payloads.",
        "tools_called": [
            {"name": "search_address"},
            {"name": "retrieve_dataset_docs"},
            {"name": "search_address"},
            {"name": "compare_neighborhoods"},
        ],
        "evidence": [{"source": "compare_neighborhoods", "detail": "four scores"}],
    }

    result = evaluation.grade_case(case, response)

    assert result["passed"] is True
    assert all(result["checks"].values())


def test_grade_case_rejects_wrong_order_forbidden_tool_and_missing_evidence():
    case = {
        "id": "bad_compare",
        "intent": "new_analysis",
        "ordered_tools": ["search_address", "compare_neighborhoods"],
        "forbidden_tools": ["query_dataset"],
        "evidence_required": True,
    }
    response = {
        "answer": "Done",
        "tools_called": ["compare_neighborhoods", "search_address", "query_dataset"],
        "evidence": [],
    }

    result = evaluation.grade_case(case, response)

    assert result["passed"] is False
    assert result["checks"]["ordered_tools"] is False
    assert result["checks"]["forbidden_tools"] is False
    assert result["checks"]["evidence_present"] is False


def test_empty_all_tools_means_no_tool_calls_expected():
    case = {
        "id": "help",
        "intent": "meta_help",
        "all_tools": [],
        "evidence_required": False,
    }

    assert evaluation.grade_case(case, {"answer": "NYC help", "tools_called": []})["passed"]
    assert not evaluation.grade_case(
        case, {"answer": "NYC help", "tools_called": ["score_neighborhood"]}
    )["passed"]


def test_validate_corpus_rejects_unknown_tool():
    corpus = {
        "schema_version": "1.0",
        "cases": [
            {
                "id": f"case-{number}",
                "intent": "meta_help",
                "prompt": "help",
                "all_tools": ["raw_sql"] if number == 0 else [],
                "evidence_required": False,
            }
            for number in range(20)
        ],
    }

    with pytest.raises(ValueError, match="unknown tools"):
        evaluation.validate_corpus(corpus)
