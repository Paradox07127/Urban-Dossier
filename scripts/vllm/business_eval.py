#!/usr/bin/env python3
"""Fixed business evaluation set for the Urban Dossier agent — EXPANSION_PLAN 4.1.

Runs the cases in evals/agent/model_cases.json through the REAL production agent
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


def failure_reasons(
    report: dict[str, Any],
    routing_results: list[dict],
    require_pass_k: bool = False,
) -> list[str]:
    """Everything that makes this run unfit to decide anything. Pure.

    An UNREACHABLE endpoint records {"error": ...} and no "results" key, so
    counting only per-case statuses meant a run where every endpoint was
    down exited 0 -- "no failures found" is exactly the wrong thing to tell
    a promotion gate that just benchmarked nothing.

    require_pass_k additionally fails a case that passed some attempts and
    not others. It is opt-in because an unreliable case and a broken one
    deserve different responses, and a gate that cannot be green while a
    known defect is open stops being read.
    """
    reasons: list[str] = []
    # Routing is model-independent and is copied into every endpoint's
    # results, so count it once here and skip that category below.
    for result in routing_results:
        if result["status"] != "pass":
            reasons.append(f"routing {result['id']}: {result['failures']}")
    for name, entry in (report.get("endpoints") or {}).items():
        if entry.get("error"):
            reasons.append(f"{name}: endpoint unreachable ({entry['error']})")
            continue
        model_results = [
            r for r in (entry.get("results") or []) if r.get("category") != "routing"
        ]
        # A run where every case was gated away benchmarked nothing, and an
        # endpoint that reports only skips must not read as a clean sheet.
        executed_results = [r for r in model_results if r.get("status") != "skip"]
        if not executed_results:
            reasons.append(f"{name}: no runnable cases executed")
        for result in model_results:
            if result["status"] in ("fail", "error"):
                reasons.append(
                    f"{name} {result['id']}: {result['status']} "
                    f"{result.get('failures')}"
                )
            elif require_pass_k and result.get("pass_hat_k") == 0.0:
                reasons.append(
                    f"{name} {result['id']}: pass^k miss "
                    f"{result.get('attempt_statuses')} "
                    f"{result.get('failures_any_attempt')}"
                )
    return reasons


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


TRACE_RESULT_CHARS = 400
TURN_TEXT_CHARS = 800


def trace_digest(
    trace: list[dict[str, Any]] | None, result_chars: int = TRACE_RESULT_CHARS
) -> list[dict[str, Any]]:
    """Compact the ACTION record for the summary report.

    Arguments are kept whole -- they are small, and "what radius did it
    actually query" is the first question anyone asks of a surprising
    result. Tool results are the bulky half (a single score_neighborhood
    payload runs ~59k chars), so they are reduced to their shape plus a
    head. Full fidelity lives in the --responses JSONL.
    """

    digest: list[dict[str, Any]] = []
    for entry in trace or []:
        result = entry.get("result")
        if isinstance(result, dict):
            encoded = json.dumps(result, default=str)
            preview: dict[str, Any] = {
                "keys": sorted(result.keys()),
                "chars": len(encoded),
            }
            if "error" in result:
                preview["error"] = result["error"]
            if len(encoded) <= result_chars:
                preview["result"] = result
            else:
                preview["result_head"] = encoded[:result_chars]
        else:
            preview = {"result": result}
        digest.append(
            {
                "iteration": entry.get("iteration"),
                "tool_name": entry.get("tool_name"),
                "args": entry.get("args"),
                "latency_ms": entry.get("latency_ms"),
                "result_preview": preview,
            }
        )
    return digest


def turns_digest(
    turns: list[dict[str, Any]] | None, text_chars: int = TURN_TEXT_CHARS
) -> list[dict[str, Any]]:
    """Compact the DELIBERATION record: what it was thinking each turn."""

    def _clip(value: Any) -> str:
        text = value if isinstance(value, str) else ""
        return text if len(text) <= text_chars else text[:text_chars] + "..."

    return [
        {
            "iteration": turn.get("iteration"),
            "kind": turn.get("kind"),
            "finish_reason": turn.get("finish_reason"),
            "tool_calls": turn.get("tool_calls"),
            "reasoning": _clip(turn.get("reasoning")),
            "content": _clip(turn.get("content")),
            "reasoning_chars": len(turn.get("reasoning") or ""),
        }
        for turn in turns or []
    ]


def grade_response(
    case: dict[str, Any], response: dict[str, Any], wall_s: float, usage: dict[str, Any]
) -> dict[str, Any]:
    """Build one graded case record from a raw run_agent response.

    Split out from run_model_case so --regrade can produce byte-identical
    records from a stored response without spending another model run --
    the same collection/grading separation scripts/evaluate_agent_business.py
    already uses for the service-level corpus.
    """

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
            "wall_s": round(wall_s, 2),
            "iterations": response.get("iterations"),
            "llm_calls": usage.get("llm_calls"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "tools_called": response.get("tools_called"),
            "tool_errors": tool_errors,
        },
        "answer": response.get("answer"),
        "trace": trace_digest(response.get("trace")),
        "turns": turns_digest(response.get("turns")),
    }


PASSING = ("pass", "warn")


def merge_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse k attempts at one case into a single record. Pure.

    `status` stays the FIRST attempt's, so a --repeat 1 report is identical
    to what this harness has always produced and a repeated run stays
    comparable to the single runs in the history.

    `pass_hat_k` is tau-bench's pass^k, not an average: 1.0 only if every
    one of the k independent attempts passed. Averaging is what let
    "compare_neighborhoods is flaky" stand for a day as a property of the
    benchmark when it was a reproducible defect in one model -- a metric
    that rounds a 2-of-3 up to "mostly fine" cannot tell those apart.
    """

    if not attempts:
        return {}
    primary = dict(attempts[0])
    if len(attempts) == 1:
        primary["pass_hat_k"] = 1.0 if primary["status"] in PASSING else 0.0
        primary["attempts"] = 1
        return primary

    statuses = [a["status"] for a in attempts]
    primary["attempts"] = len(attempts)
    primary["attempt_statuses"] = statuses
    primary["pass_hat_k"] = (
        1.0 if all(s in PASSING for s in statuses) else 0.0
    )
    # Union of what went wrong across attempts -- a failure seen in any run
    # is a failure the promotion has to answer for.
    seen: list[str] = []
    for attempt in attempts:
        for failure in attempt.get("failures") or []:
            if failure not in seen:
                seen.append(failure)
    primary["failures_any_attempt"] = seen
    walls = [
        a["metrics"]["wall_s"] for a in attempts if (a.get("metrics") or {}).get("wall_s")
    ]
    if walls:
        primary["wall_s_attempts"] = walls
    return primary


def summarize(results: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    """Endpoint-level roll-up. Pure, so --regrade produces the same shape."""

    counted = [r for r in results if r["status"] in ("pass", "warn", "fail")]
    passed = [r for r in counted if r["status"] in PASSING]
    walls = sorted(r["metrics"]["wall_s"] for r in results if r.get("metrics"))
    return {
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "skip": sum(1 for r in results if r["status"] == "skip"),
        "error": sum(1 for r in results if r["status"] == "error"),
        "pass_rate": round(len(passed) / len(counted), 3) if counted else None,
        "wall_p50_s": walls[len(walls) // 2] if walls else None,
        "wall_max_s": walls[-1] if walls else None,
        # `or 0`: a replayed record whose usage was never captured must not
        # take the whole summary down with a None.
        "completion_tokens_total": sum(
            (r["metrics"].get("completion_tokens") or 0)
            for r in results
            if r.get("metrics")
        ),
        "repeat": repeat,
        # Fraction of cases that passed on EVERY attempt. Equals pass_rate
        # when repeat=1; diverges from it exactly where the model is
        # unreliable rather than wrong.
        "pass_hat_k": (
            round(
                sum(1 for r in counted if r.get("pass_hat_k") == 1.0) / len(counted), 3
            )
            if counted
            else None
        ),
    }


def regrade_responses(
    path: Path, cases_by_id: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Re-grade a stored --responses JSONL with the CURRENT graders.

    The point of keeping the raw responses is that a grader bug found later
    can be re-run over every past decision without paying for the model
    time again -- and, more importantly, without the models having drifted
    underneath the comparison.
    """

    by_endpoint: dict[str, dict[str, list]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        case = cases_by_id.get(entry.get("case_id"))
        if case is None or "response" not in entry:
            continue
        bucket = by_endpoint.setdefault(
            entry.get("endpoint", "replay"),
            {"model": entry.get("model", ""), "attempts": {}},
        )
        record = grade_response(
            case, entry["response"], entry.get("wall_s", 0.0), entry.get("usage") or {}
        )
        bucket["attempts"].setdefault(case["id"], []).append(record)

    out: dict[str, dict[str, Any]] = {}
    for endpoint_name, bucket in by_endpoint.items():
        results = [merge_attempts(a) for a in bucket["attempts"].values()]
        repeat = max((len(a) for a in bucket["attempts"].values()), default=1)
        out[endpoint_name] = {
            "url": "(replayed)",
            "model": bucket["model"],
            "summary": summarize(results, repeat),
            "results": results,
        }
    return out


def run_model_case(
    case: dict[str, Any], base_url: str, model: str, max_iterations: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one case. Returns (graded record, raw record for the JSONL)."""

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
    usage = {
        "llm_calls": tracker.llm_calls,
        "prompt_tokens": tracker.prompt_tokens,
        "completion_tokens": tracker.completion_tokens,
    }
    record = grade_response(case, response, wall, usage)
    raw = {
        "case_id": case["id"],
        "wall_s": round(wall, 2),
        "usage": usage,
        "response": response,
    }
    return record, raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", action="append", metavar="NAME=URL", default=None)
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--ids", default=None, help="comma-separated case id filter")
    parser.add_argument("--routing-only", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--output", default=None)
    parser.add_argument(
        "--repeat", type=int, default=1, metavar="K",
        help="run each case K times per endpoint and report pass^k "
             "(tau-bench sense: a case counts only if all K attempts pass)",
    )
    parser.add_argument(
        "--require-pass-k", action="store_true",
        help="make pass^k part of the exit-code contract. Off by default: "
             "a known-open defect would otherwise redden every run.",
    )
    parser.add_argument(
        "--responses", default=None, metavar="PATH",
        help="write one JSON object per attempt, full fidelity, to this "
             "JSONL. Replayable with --regrade after a grader change.",
    )
    parser.add_argument(
        "--regrade", default=None, metavar="PATH",
        help="grade a previously collected --responses JSONL instead of "
             "calling any model. Costs no GPU time.",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")
    if args.regrade and args.endpoint:
        parser.error("--regrade replays a stored run; it takes no --endpoint")

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

    if args.regrade:
        by_id = {c["id"]: c for c in model_cases}
        for endpoint_name, endpoint_report in regrade_responses(
            Path(args.regrade), by_id
        ).items():
            endpoint_report["results"] = (
                list(routing_results) + endpoint_report["results"]
            )
            report["endpoints"][endpoint_name] = endpoint_report
            print(f"[{endpoint_name}] regraded {json.dumps(endpoint_report['summary'])}")
        return _finish(report, routing_results, args)

    responses_fh = None
    if args.responses:
        responses_path = Path(args.responses)
        responses_path.parent.mkdir(parents=True, exist_ok=True)
        responses_fh = responses_path.open("w", encoding="utf-8")

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
            attempts: list[dict[str, Any]] = []
            for attempt in range(1, args.repeat + 1):
                try:
                    record, raw = run_model_case(
                        case, url, model, args.max_iterations
                    )
                except Exception as exc:  # noqa: BLE001 - harness must survive
                    record = {
                        "id": case["id"],
                        "category": case["category"],
                        "status": "error",
                        "failures": [f"{type(exc).__name__}: {exc}"],
                    }
                    raw = {"case_id": case["id"], "error": str(exc)}
                attempts.append(record)
                if responses_fh is not None:
                    responses_fh.write(
                        json.dumps(
                            {"endpoint": name, "model": model, "attempt": attempt, **raw},
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
                metrics = record.get("metrics") or {}
                suffix = f" [{attempt}/{args.repeat}]" if args.repeat > 1 else ""
                print(
                    f"[{name}] {record['id']}{suffix}: {record['status']}"
                    f"  ({metrics.get('wall_s', '-')}s,"
                    f" tools={metrics.get('tools_called', [])})"
                    + (f"  {record['failures']}" if record.get("failures") else "")
                )

            result = merge_attempts(attempts)
            results.append(result)
            if args.repeat > 1 and result["pass_hat_k"] < 1.0:
                statuses = [a["status"] for a in attempts]
                print(f"[{name}] {result['id']}: pass^{args.repeat} MISS {statuses}")

        summary = summarize(results, args.repeat)
        print(f"[{name}] {json.dumps(summary)}")
        report["endpoints"][name] = {
            "url": url,
            "model": model,
            "summary": summary,
            "results": results,
        }

    if responses_fh is not None:
        responses_fh.close()
        print(f"responses written to {args.responses}")

    return _finish(report, routing_results, args)


def _finish(report: dict[str, Any], routing_results: list[dict], args: Any) -> int:
    """Write the report and turn it into an exit code. Shared by both paths."""

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

    reasons = failure_reasons(
        report, routing_results, require_pass_k=args.require_pass_k
    )
    for reason in reasons:
        print(f"FAIL {reason}", file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
