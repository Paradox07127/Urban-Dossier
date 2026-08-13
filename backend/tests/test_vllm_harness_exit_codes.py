"""Exit-code contracts for the two vLLM harnesses.

Both scripts wrote honest reports and then returned 0 no matter what, so a
run where every endpoint was down told any calling script "no problems
found". These pin the inverse: the exit code is only 0 when the run is
actually fit to decide something.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "vllm" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ab_bench = _load("ab_bench")
business_eval = _load("business_eval")


def _level(concurrency=1, requests=8, errors=None):
    return {
        "concurrency": concurrency,
        "requests": requests,
        "errors": errors or [],
        "wall_s": 12.0,
    }


# --- ab_bench ---------------------------------------------------------------


def test_ab_bench_clean_run_has_no_reasons():
    report = {"endpoints": {"current": {"levels": [_level(), _level(4)]}}}
    assert ab_bench.failure_reasons(report, expected_requests=8) == []


def test_ab_bench_flags_unreachable_endpoint():
    report = {
        "endpoints": {
            "current": {"levels": [_level()]},
            "lightning": {"url": "http://127.0.0.1:8002", "error": "connection refused"},
        }
    }
    reasons = ab_bench.failure_reasons(report, expected_requests=8)
    assert len(reasons) == 1
    assert "lightning" in reasons[0] and "unreachable" in reasons[0]


def test_ab_bench_flags_request_errors_and_partial_completion():
    report = {
        "endpoints": {
            "current": {
                "levels": [
                    _level(errors=["timeout"]),
                    _level(concurrency=4, requests=5),
                ]
            }
        }
    }
    reasons = ab_bench.failure_reasons(report, expected_requests=8)
    assert any("request error" in r for r in reasons)
    assert any("5/8 requests" in r for r in reasons)


def test_ab_bench_flags_a_run_that_benchmarked_nothing():
    assert ab_bench.failure_reasons({"endpoints": {}}, expected_requests=8)


# --- business_eval ----------------------------------------------------------


def _case(case_id, status, category="tool_call"):
    return {"id": case_id, "category": category, "status": status, "failures": []}


ROUTING_OK = [_case("route-a", "pass", "routing")]


def test_business_eval_clean_run_has_no_reasons():
    report = {
        "endpoints": {
            "current": {"results": ROUTING_OK + [_case("c1", "pass"), _case("c2", "warn")]}
        }
    }
    assert business_eval.failure_reasons(report, ROUTING_OK) == []


def test_business_eval_flags_unreachable_endpoint():
    """The regression: no "results" key at all used to mean "nothing failed"."""
    report = {"endpoints": {"lightning": {"url": "http://x", "error": "refused"}}}
    reasons = business_eval.failure_reasons(report, ROUTING_OK)
    assert len(reasons) == 1
    assert "unreachable" in reasons[0]


def test_business_eval_flags_failures_and_errors_but_not_skips():
    report = {
        "endpoints": {
            "current": {
                "results": ROUTING_OK
                + [_case("c1", "fail"), _case("c2", "error"), _case("c3", "skip")]
            }
        }
    }
    reasons = business_eval.failure_reasons(report, ROUTING_OK)
    assert len(reasons) == 2
    assert not any("c3" in r for r in reasons)


def test_business_eval_counts_routing_failures_once_not_per_endpoint():
    routing = [_case("route-a", "fail", "routing")]
    report = {
        "endpoints": {
            "current": {"results": routing + [_case("c1", "pass")]},
            "lightning": {"results": routing + [_case("c1", "pass")]},
        }
    }
    reasons = business_eval.failure_reasons(report, routing)
    assert len(reasons) == 1


def test_business_eval_flags_an_endpoint_that_ran_no_cases():
    report = {"endpoints": {"current": {"results": list(ROUTING_OK)}}}
    reasons = business_eval.failure_reasons(report, ROUTING_OK)
    assert any("no cases ran" in r for r in reasons)
