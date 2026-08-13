#!/usr/bin/env python3
"""Fixed business evaluation set for the Urban Dossier agent — EXPANSION_PLAN 4.1.

Runs the cases in evals/agent/cases.json through the REAL production agent
loop (`urban_dossier_analyst.agent_loop.run_agent`: production system prompt,
production tools, production budgets) pointed at any OpenAI-compatible
endpoint, and grades every response. The point is a fair, repeatable surface
for model decisions: same prompts, same tools, swap only the endpoint.
No model switch, KV-cache change, or prompt rewrite should be decided
without citing a run of this set.

Tool dispatch uses the skill's HTTP loopback against the live backend
(http://localhost:8090 by default), i.e. the same path the sandboxed agent
takes in production. The backend must be up.

Usage:
    python3 scripts/vllm/business_eval.py \
        --endpoint current=http://127.0.0.1:8000 \
        --endpoint lightning=http://127.0.0.1:8002 \
        --output /mnt/data/urban-dossier-state/evals/business_eval.json

    # Routing-only smoke (no model, no GPU):
    python3 scripts/vllm/business_eval.py --routing-only

Grading model:
  pass   every hard check held
  warn   hard checks held; a soft check (numeric faithfulness) did not
  fail   a hard check failed
  skip   the case needs an availability-gated tool that is not released
  error  the harness itself failed (exception, endpoint down)

Numeric faithfulness (soft, reported per case and in aggregate): every
number of two or more digits in the final answer must appear either in the
user prompt or in some tool result / evidence entry. It is soft because
legitimate derived figures (differences, percentages) fail string matching —
but a model that fails it wholesale is inventing numbers, which is exactly
the behavior that separated the candidates in the 2026-08 A/B.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "skills"))

from urban_dossier_analyst.routing import route_intent  # noqa: E402

CASES_PATH = REPO_ROOT / "evals" / "agent" / "model_cases.json"
CITATION_RE = re.compile(r"\[[^\[\]]{3,120}\]")
# Two-plus-digit integers and any decimal: small counts like "3 datasets"
# are conversational, not claims worth policing.
NUMBER_RE = re.compile(r"\d+\.\d+|\d{2,}")

# Models emit typographic punctuation ("isn’t", "‑73.99" with a non-breaking
# hyphen); the case regexes are ASCII. Observed in the first Nano baseline: a
# perfect refusal failed grading because of U+2019. Normalize before matching.
_TYPOGRAPHIC_MAP = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    " ": " ",
})


def _canon(text: str) -> str:
    return (text or "").translate(_TYPOGRAPHIC_MAP)


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #


def _numbers(text: str) -> set[str]:
    return set(NUMBER_RE.findall(text or ""))


def _sentence_count(text: str) -> int:
    # Strip decimals and common abbreviations so "40.72" or "e.g." do not
    # count as sentence boundaries.
    cleaned = re.sub(r"\d+\.\d+", "0", text or "")
    cleaned = re.sub(r"\b(e\.g|i\.e|vs|approx|St|Ave)\.", r"\1", cleaned)
    parts = [p for p in re.split(r"[.!?。！？]+", cleaned) if p.strip()]
    return len(parts)


def _extract_json(text: str) -> dict[str, Any] | None:
    body = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _args_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, float) and isinstance(got, (int, float)):
            if abs(float(got) - want) > 0.01:
                return False
        elif got != want:
            return False
    return True


def grade_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Grade one agent response against the case's `expect` block.

    Returns {"status": pass|warn|fail, "failures": [...], "soft": {...}}.
    Pure, so the graders themselves are unit-testable without a model.
    """
    expect = case.get("expect", {})
    prompt = case["prompt"]
    answer = _canon(response.get("answer") or "")
    tools_called = response.get("tools_called") or []
    trace = response.get("trace") or []
    evidence = response.get("evidence") or []
    failures: list[str] = []

    for tool in expect.get("tools_all", []):
        if tool not in tools_called:
            failures.append(f"required tool not called: {tool}")
    if expect.get("tools_any") and not set(expect["tools_any"]) & set(tools_called):
        failures.append(f"none of {expect['tools_any']} called (got {tools_called})")
    for tool in expect.get("tools_forbidden", []):
        if tool in tools_called:
            failures.append(f"forbidden tool called: {tool}")

    order_pairs = expect.get("order", [])
    if order_pairs:
        # any_present: only pairs whose second tool was actually used must be
        # ordered (e.g. search_address before WHICHEVER spatial tool ran).
        any_present = expect.get("order_mode") == "any_present"
        for first, second in order_pairs:
            if first in tools_called and second in tools_called:
                if tools_called.index(first) > tools_called.index(second):
                    failures.append(f"order violated: {first} must precede {second}")
            elif not any_present:
                failures.append(f"order pair incomplete: {first} -> {second}")

    for tool, wanted_args in expect.get("args_contain", {}).items():
        calls = [t for t in trace if t.get("tool_name") == tool]
        if not calls:
            failures.append(f"args check: {tool} never called")
        elif not any(_args_match(wanted_args, t.get("args") or {}) for t in calls):
            failures.append(
                f"args mismatch for {tool}: wanted {wanted_args}, "
                f"got {[t.get('args') for t in calls]}"
            )

    if len(tools_called) < expect.get("min_tool_calls", 0):
        failures.append(
            f"expected >= {expect['min_tool_calls']} tool calls, got {len(tools_called)}"
        )

    for pattern in expect.get("answer_regex_all", []):
        if not re.search(pattern, answer, re.IGNORECASE):
            failures.append(f"answer missing required pattern: {pattern}")
    if expect.get("answer_regex_any"):
        if not any(
            re.search(p, answer, re.IGNORECASE) for p in expect["answer_regex_any"]
        ):
            failures.append(
                f"answer matched none of the acceptable patterns: "
                f"{expect['answer_regex_any']}"
            )
    for pattern in expect.get("answer_forbidden_regex", []):
        if re.search(pattern, answer, re.IGNORECASE):
            failures.append(f"answer matched forbidden pattern: {pattern}")

    if expect.get("citation_required") and not (CITATION_RE.search(answer) or evidence):
        failures.append("no inline [source] citation and empty evidence list")
    if expect.get("evidence_list_required") and not evidence:
        failures.append("structured evidence list is empty")

    if expect.get("json_answer_keys"):
        parsed = _extract_json(answer)
        if parsed is None:
            failures.append("answer contains no parseable JSON object")
        else:
            missing = [k for k in expect["json_answer_keys"] if k not in parsed]
            if missing:
                failures.append(f"JSON answer missing keys: {missing}")

    if expect.get("max_sentences") is not None:
        count = _sentence_count(answer)
        if count > expect["max_sentences"]:
            failures.append(
                f"answer has {count} sentences, limit {expect['max_sentences']}"
            )

    either = expect.get("either")
    if either:
        tools_ok = bool(set(either.get("tools_any", [])) & set(tools_called))
        regex_ok = any(
            re.search(p, answer, re.IGNORECASE)
            for p in either.get("answer_regex_any", [])
        )
        if not (tools_ok or regex_ok):
            failures.append(f"neither branch of `either` satisfied: {either}")

    if expect.get("no_numbers_without_tools") and not tools_called:
        invented = _numbers(answer) - _numbers(prompt)
        if invented:
            failures.append(f"numeric claims with zero tool calls: {sorted(invented)}")

    # Soft: numeric faithfulness.
    soft: dict[str, Any] = {}
    if expect.get("numeric_faithfulness"):
        allowed = _numbers(prompt)
        allowed |= _numbers(json.dumps(trace, default=str))
        allowed |= _numbers(json.dumps(evidence, default=str))
        claimed = _numbers(answer)
        offenders = sorted(claimed - allowed)
        soft["faithfulness"] = {
            "claimed": len(claimed),
            "unsupported": offenders,
            "ratio": (
                round((len(claimed) - len(offenders)) / len(claimed), 3)
                if claimed
                else 1.0
            ),
        }

    if failures:
        status = "fail"
    elif soft.get("faithfulness", {}).get("unsupported"):
        status = "warn"
    else:
        status = "pass"
    return {"status": status, "failures": failures, "soft": soft}


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class _UsageTracker:
    """client_factory seam: wraps the real OpenAI client and accumulates
    token usage across every completion call run_agent makes."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.llm_calls = 0

    def factory(self, base_url: str) -> Any:
        from openai import OpenAI

        inner = OpenAI(base_url=base_url, api_key="vllm-no-auth")
        tracker = self

        class _Completions:
            def create(self, **kwargs: Any) -> Any:
                response = inner.chat.completions.create(**kwargs)
                tracker.llm_calls += 1
                usage = getattr(response, "usage", None)
                if usage is not None:
                    tracker.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                    tracker.completion_tokens += (
                        getattr(usage, "completion_tokens", 0) or 0
                    )
                return response

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        return _Client()


def get_model_id(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url}/v1/models", timeout=30) as resp:
        return json.load(resp)["data"][0]["id"]


def run_routing_case(case: dict[str, Any]) -> dict[str, Any]:
    route = route_intent(case["prompt"])
    expect = case["expect"]
    failures = []
    if route.intent.value != expect["route_intent"]:
        failures.append(
            f"intent {route.intent.value!r} != expected {expect['route_intent']!r}"
        )
    if expect.get("route_rule") and route.rule != expect["route_rule"]:
        failures.append(f"rule {route.rule!r} != expected {expect['route_rule']!r}")
    return {
        "id": case["id"],
        "category": case["category"],
        "status": "fail" if failures else "pass",
        "failures": failures,
        "route": {"intent": route.intent.value, "rule": route.rule},
    }


def run_model_case(
    case: dict[str, Any], base_url: str, model: str, max_iterations: int
) -> dict[str, Any]:
    from urban_dossier_analyst.agent_loop import run_agent

    tracker = _UsageTracker()
    started = time.monotonic()
    response = run_agent(
        user_message=case["prompt"],
        max_iterations=max_iterations,
        vllm_base_url=f"{base_url}/v1",
        model=model,
        client_factory=tracker.factory,
    )
    wall = time.monotonic() - started
    graded = grade_case(case, response)
    tool_errors = sum(
        1
        for entry in response.get("trace") or []
        if isinstance(entry.get("result"), dict) and "error" in entry["result"]
    )
    return {
        "id": case["id"],
        "category": case["category"],
        "status": graded["status"],
        "failures": graded["failures"],
        "soft": graded["soft"],
        "metrics": {
            "wall_s": round(wall, 2),
            "iterations": response.get("iterations"),
            "llm_calls": tracker.llm_calls,
            "prompt_tokens": tracker.prompt_tokens,
            "completion_tokens": tracker.completion_tokens,
            "tools_called": response.get("tools_called"),
            "tool_errors": tool_errors,
        },
        "answer": response.get("answer"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", action="append", metavar="NAME=URL", default=None)
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--ids", default=None, help="comma-separated case id filter")
    parser.add_argument("--routing-only", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    spec = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = spec["cases"]
    if args.ids:
        wanted = {token.strip() for token in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in wanted]

    # Availability gates resolve URBAN_DOSSIER_DATA_ROOT with a cwd-relative
    # default ("data"), so a run from outside the production checkout silently
    # loses gated tools (observed: simulate_intervention). Anchor the default
    # to this repo before the tool layer is imported; an explicit env var
    # still wins.
    import os

    default_data_root = REPO_ROOT / "data"
    if "URBAN_DOSSIER_DATA_ROOT" not in os.environ and default_data_root.is_dir():
        os.environ["URBAN_DOSSIER_DATA_ROOT"] = str(default_data_root)

    from urban_dossier_analyst.tools import tool_availability

    availability = tool_availability()
    available = {
        name
        for name, state in availability.items()
        if (state.get("available") if isinstance(state, dict) else bool(state))
    }

    report: dict[str, Any] = {
        "eval_schema": spec.get("schema_version"),
        "cases_total": len(cases),
        "generated_unix": int(time.time()),
        "tool_availability": sorted(available),
        "endpoints": {},
    }

    routing_cases = [c for c in cases if c["category"] == "routing"]
    model_cases = [c for c in cases if c["category"] != "routing"]
    routing_results = [run_routing_case(c) for c in routing_cases]
    for result in routing_results:
        print(f"[routing] {result['id']}: {result['status']}"
              + (f"  {result['failures']}" if result["failures"] else ""))

    endpoints = args.endpoint or (
        [] if args.routing_only else ["current=http://127.0.0.1:8000"]
    )
    for endpoint_spec in endpoints:
        name, _, url = endpoint_spec.partition("=")
        url = url.rstrip("/")
        if not url:
            parser.error(f"--endpoint needs NAME=URL, got {endpoint_spec}")
        try:
            model = get_model_id(url)
        except OSError as exc:
            print(f"[{name}] UNREACHABLE at {url}: {exc}", file=sys.stderr)
            report["endpoints"][name] = {"url": url, "error": str(exc)}
            continue
        print(f"[{name}] {model} at {url}: {len(model_cases)} cases")

        results: list[dict[str, Any]] = list(routing_results)
        for case in model_cases:
            missing = [t for t in case.get("requires_tools", []) if t not in available]
            if missing:
                results.append(
                    {
                        "id": case["id"],
                        "category": case["category"],
                        "status": "skip",
                        "failures": [f"tool(s) not released: {missing}"],
                    }
                )
                print(f"[{name}] {case['id']}: skip (needs {missing})")
                continue
            try:
                result = run_model_case(case, url, model, args.max_iterations)
            except Exception as exc:  # noqa: BLE001 - harness must survive
                result = {
                    "id": case["id"],
                    "category": case["category"],
                    "status": "error",
                    "failures": [f"{type(exc).__name__}: {exc}"],
                }
            results.append(result)
            metrics = result.get("metrics") or {}
            print(
                f"[{name}] {result['id']}: {result['status']}"
                f"  ({metrics.get('wall_s', '-')}s,"
                f" tools={metrics.get('tools_called', [])})"
                + (f"  {result['failures']}" if result.get("failures") else "")
            )

        counted = [r for r in results if r["status"] in ("pass", "warn", "fail")]
        passed = [r for r in counted if r["status"] in ("pass", "warn")]
        walls = sorted(
            (r["metrics"]["wall_s"] for r in results if r.get("metrics")),
        )
        summary = {
            "pass": sum(1 for r in results if r["status"] == "pass"),
            "warn": sum(1 for r in results if r["status"] == "warn"),
            "fail": sum(1 for r in results if r["status"] == "fail"),
            "skip": sum(1 for r in results if r["status"] == "skip"),
            "error": sum(1 for r in results if r["status"] == "error"),
            "pass_rate": round(len(passed) / len(counted), 3) if counted else None,
            "wall_p50_s": walls[len(walls) // 2] if walls else None,
            "wall_max_s": walls[-1] if walls else None,
            "completion_tokens_total": sum(
                r["metrics"]["completion_tokens"] for r in results if r.get("metrics")
            ),
        }
        print(f"[{name}] {json.dumps(summary)}")
        report["endpoints"][name] = {
            "url": url,
            "model": model,
            "summary": summary,
            "results": results,
        }

    if args.routing_only:
        report["routing_results"] = routing_results
        failed = [r for r in routing_results if r["status"] != "pass"]
        print(f"routing: {len(routing_results) - len(failed)}/{len(routing_results)} pass")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"report written to {out}")

    any_fail = any(
        r["status"] in ("fail", "error")
        for entry in report["endpoints"].values()
        for r in entry.get("results", [])
    ) or any(r["status"] != "pass" for r in routing_results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
