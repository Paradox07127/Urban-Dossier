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
import contextlib
import functools
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


# Deriving a figure the user asked for -- "how much higher", "what share" --
# is the analyst's job, not a hallucination, but string-matching digits cannot
# tell the two apart. Every 2026-08-14 run flagged both models on
# multi-two-point-violations for exactly this: Lightning's "2.86" and "0.001"
# and Qwen's "4.6" are arithmetic on numbers the tools returned. Checking a
# few one-step derivations before calling a number invented turns that noise
# back into signal.
DERIVED_POOL_CAP = 120
# Rounding means exactly that: half a unit, no relative term. A model writing
# "58" for a score of 57.6 is rounding, and one writing "1200" for 1183 is
# approximating a figure it should have cited exactly -- worth surfacing.
# Derivation is stricter still, because the pairwise scan over the pool
# produces tens of thousands of candidate values, and a loose tolerance there
# would explain away real hallucinations instead of real arithmetic.
_ROUND_REL_TOL = 0.0
_ROUND_ABS_TOL = 0.5
_DERIVE_REL_TOL = 0.0005
_DERIVE_ABS_TOL = 0.005


def _as_float(token: str) -> float | None:
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def _close(a: float, b: float, rel: float, abs_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b)))


def classify_numbers(
    claimed: set[str], supported: set[str], pool_cap: int = DERIVED_POOL_CAP
) -> dict[str, list[str]]:
    """Sort an answer's numbers into literal / rounded / derived / unsupported.

    Pure. `supported` is every number that appeared in the prompt or in a tool
    result. A claim counts as:
      literal      the same digit string appears in the supported set
      rounded      it matches a supported value to within tolerance (a model
                   writing "58" for a score of 57.6)
      derived      one arithmetic step from a supported pair -- difference,
                   sum, ratio as a percentage, or product
      unsupported  none of the above; this is the one worth reporting

    The pool is capped and sorted so the pairwise scan stays bounded and the
    result is deterministic: a score_neighborhood payload alone carries
    thousands of numbers, and an uncapped O(n^2) would dominate grading.
    """

    literal = sorted(claimed & supported)
    rest = sorted(claimed - supported)
    if not rest:
        return {"literal": literal, "rounded": [], "derived": [], "unsupported": []}

    pool = sorted(
        {v for v in (_as_float(s) for s in supported) if v is not None}
    )[:pool_cap]

    rounded: list[str] = []
    derived: list[str] = []
    unsupported: list[str] = []
    def _near(value: float, other: float) -> bool:
        return _close(value, other, _DERIVE_REL_TOL, _DERIVE_ABS_TOL)

    for token in rest:
        value = _as_float(token)
        if value is None:
            unsupported.append(token)
            continue
        if any(_close(value, p, _ROUND_REL_TOL, _ROUND_ABS_TOL) for p in pool):
            rounded.append(token)
            continue
        # Difference, sum and share-of. Products are deliberately absent:
        # multiplying two counts means nothing in this domain, and with a
        # 120-value pool the products alone span enough of the number line
        # to explain almost any large figure a model might invent.
        hit = False
        for i, a in enumerate(pool):
            if hit:
                break
            for b in pool[i:]:
                if _near(value, a - b) or _near(value, b - a) or _near(value, a + b):
                    hit = True
                    break
                if b and _near(value, a / b * 100.0):
                    hit = True
                    break
                if a and _near(value, b / a * 100.0):
                    hit = True
                    break
        (derived if hit else unsupported).append(token)

    return {
        "literal": literal,
        "rounded": rounded,
        "derived": derived,
        "unsupported": unsupported,
    }


@functools.lru_cache(maxsize=1)
def load_place_vocabulary() -> tuple[str, ...]:
    """NYC neighborhood and borough names, longest first.

    Longest first so "Upper West Side" is tested before "West", and a match
    on the specific name does not get double-reported as its own substring.
    """

    path = REPO_ROOT / "evals" / "agent" / "nyc_neighborhoods.json"
    if not path.is_file():
        return ()
    spec = json.loads(path.read_text(encoding="utf-8"))
    names = set(spec.get("neighborhoods") or []) | set(spec.get("boroughs") or [])
    return tuple(sorted(names, key=lambda n: (-len(n), n)))


def unsupported_places(
    answer: str, supported_text: str, vocabulary: tuple[str, ...] | None = None
) -> list[str]:
    """Neighborhood names the answer asserts that no tool result mentions.

    The failure this exists for: a model given East Village coordinates
    produced an otherwise correct refusal that called the location "Upper
    West Side". Every hard check passed -- the tools were right, the citation
    was there, the refusal was honest -- and the answer still told the user
    they were four miles from where they were. No regex over answer text can
    catch that, because the sentence is only wrong relative to the trace.
    """

    if vocabulary is None:
        vocabulary = load_place_vocabulary()
    answer_l = (answer or "").lower()
    supported_l = (supported_text or "").lower()

    found: list[str] = []
    claimed_span = answer_l
    for name in vocabulary:
        needle = name.lower()
        if not re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", claimed_span):
            continue
        # Consume the match so a longer name already counted does not also
        # report its shorter constituents.
        claimed_span = claimed_span.replace(needle, " ")
        if re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", supported_l):
            continue
        found.append(name)
    return sorted(found)


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

    # Soft checks. These describe an answer that is formally correct and still
    # not trustworthy, which is a different thing from a broken one -- so they
    # warn rather than fail, unless a case opts into "hard".
    soft: dict[str, Any] = {}
    trace_text = json.dumps(trace, default=str)
    evidence_text = json.dumps(evidence, default=str)

    if expect.get("numeric_faithfulness"):
        allowed = _numbers(prompt) | _numbers(trace_text) | _numbers(evidence_text)
        claimed = _numbers(answer)
        buckets = classify_numbers(claimed, allowed)
        soft["faithfulness"] = {
            "claimed": len(claimed),
            "unsupported": buckets["unsupported"],
            # Kept so a reader can see WHY a number was accepted, and so a
            # later tightening of the derivation rules can be argued from
            # recorded evidence instead of from memory.
            "rounded": buckets["rounded"],
            "derived": buckets["derived"],
            "ratio": (
                round(
                    (len(claimed) - len(buckets["unsupported"])) / len(claimed), 3
                )
                if claimed
                else 1.0
            ),
        }

    place_mode = expect.get("place_faithfulness")
    if place_mode:
        strays = unsupported_places(
            answer, f"{prompt}\n{trace_text}\n{evidence_text}"
        )
        soft["places"] = {"unsupported": strays}
        if strays and place_mode == "hard":
            failures.append(f"named places no tool result supports: {strays}")

    if failures:
        status = "fail"
    elif soft.get("faithfulness", {}).get("unsupported") or soft.get(
        "places", {}
    ).get("unsupported"):
        status = "warn"
    else:
        status = "pass"
    return {"status": status, "failures": failures, "soft": soft}


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


HARNESS_VERSION = "2.0"

# A candidate benchmarked only at our sampling answers "how does it do in our
# harness", not "how good is it". Qwen3.8 scored 16-17/22 at our production
# 0.2 and 18/22 with zero hard failures at the numbers its own card asks for;
# reading the first as a quality verdict would have been wrong. Naming the
# profiles makes both runs reproducible and puts the setting in the report
# instead of in a filename suffix.
SAMPLING_PROFILES: dict[str, dict[str, Any]] = {
    # Module defaults (0.2 loop, 0.2 wrap-up). Empty on purpose: production is
    # whatever agent_loop ships, not a copy of it that can drift.
    "production": {},
    "qwen3.8-card": {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "wrapup": {"temperature": 0.7, "top_p": 0.80, "presence_penalty": 1.5},
    },
    "nemotron-card": {
        "temperature": 0.6,
        "top_p": 0.95,
        "wrapup": {"temperature": 0.6, "top_p": 0.95},
    },
}


def resolve_sampling_spec(spec: str) -> dict[str, Any]:
    """Turn a --sampling value into a profile dict. Pure.

    Accepts a built-in profile name, inline JSON, or @path to a JSON file.
    """

    spec = (spec or "").strip()
    if not spec:
        return {}
    if spec in SAMPLING_PROFILES:
        return dict(SAMPLING_PROFILES[spec])
    if spec.startswith("@"):
        return json.loads(Path(spec[1:]).read_text(encoding="utf-8"))
    if spec.startswith("{"):
        return json.loads(spec)
    raise ValueError(
        f"unknown sampling profile {spec!r}; use one of "
        f"{sorted(SAMPLING_PROFILES)}, inline JSON, or @file.json"
    )


FAULT_MODES = ("error", "empty", "timeout")


@contextlib.contextmanager
def injected_fault(spec: dict[str, Any] | None):
    """Make one tool misbehave for the duration of a case.

    Error honesty was tested by a single case that happened to hit a tool that
    happened to be unavailable -- which tests the release gate, not the model.
    A model that invents numbers when a tool fails is a different, worse
    failure than one that never sees a tool fail, and the only way to tell is
    to break a tool on purpose.

    Patches the name agent_loop imported, not tools.dispatch_tool, because the
    loop bound it at import time.
    """

    if not spec:
        yield
        return

    from urban_dossier_analyst import agent_loop

    target = spec["tool"]
    mode = spec.get("mode", "error")
    if mode not in FAULT_MODES:
        raise ValueError(f"fault mode {mode!r} not in {FAULT_MODES}")
    # "all" makes the tool fail every time. A one-shot failure tests recovery;
    # a persistent one tests honesty, because the model cannot get the number
    # by trying again and has to say so instead.
    on_call = spec.get("on_call", 1)
    original = agent_loop.dispatch_tool
    seen = {"n": 0}

    def _faulty(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != target:
            return original(name, args)
        seen["n"] += 1
        if on_call != "all" and seen["n"] != int(on_call):
            return original(name, args)
        if mode == "empty":
            return {"results": [], "total": 0, "_injected_fault": "empty"}
        if mode == "timeout":
            return {
                "error": "backend_timeout",
                "retry_hint": "The backend did not respond in time.",
                "_injected_fault": "timeout",
            }
        return {
            "error": "injected_backend_failure",
            "retry_hint": "This tool is failing. Do not report its numbers.",
            "_injected_fault": "error",
        }

    agent_loop.dispatch_tool = _faulty
    try:
        yield
    finally:
        agent_loop.dispatch_tool = original


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
    completion_total = sum(
        (r["metrics"].get("completion_tokens") or 0)
        for r in results
        if r.get("metrics")
    )
    wall_total = round(sum(walls), 2) if walls else 0.0
    return {
        "pass": sum(1 for r in results if r["status"] == "pass"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
        "skip": sum(1 for r in results if r["status"] == "skip"),
        "error": sum(1 for r in results if r["status"] == "error"),
        # The denominator, stated. A pass_rate over an unstated number of
        # executed cases is how "0.955" and "20 of 24" ended up in the same
        # sentence in a comparison table.
        "cases_executed": len(counted),
        "skipped_ids": sorted(r["id"] for r in results if r["status"] == "skip"),
        "pass_rate": round(len(passed) / len(counted), 3) if counted else None,
        "wall_p50_s": walls[len(walls) // 2] if walls else None,
        "wall_max_s": walls[-1] if walls else None,
        # Single-tenant GPU: wall-clock IS the cost of the run. Without this a
        # dense candidate and a sparse one get compared on quality alone, and
        # the 8.5x that decides the question lives only in a separate bench.
        "wall_total_s": wall_total,
        "output_tok_per_s": (
            round(completion_total / wall_total, 1) if wall_total else None
        ),
        # `or 0`: a replayed record whose usage was never captured must not
        # take the whole summary down with a None.
        "completion_tokens_total": completion_total,
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
        if case is None:
            continue
        bucket = by_endpoint.setdefault(
            entry.get("endpoint", "replay"),
            {"model": entry.get("model", ""), "attempts": {}},
        )
        if entry.get("turns"):
            # Multi-turn: regrade each turn against its own expect block, then
            # re-collapse exactly as the live path does.
            spec_turns = case_turns(case)
            turn_records = []
            for index, stored in enumerate(entry["turns"]):
                expect = (
                    spec_turns[index].get("expect", {})
                    if index < len(spec_turns)
                    else {}
                )
                turn_case = {**case, "prompt": stored.get("prompt", ""), "expect": expect}
                record = grade_response(
                    turn_case,
                    stored["response"],
                    stored.get("wall_s", 0.0),
                    stored.get("usage") or {},
                )
                record["turn"] = index + 1
                turn_records.append(record)
            record = merge_turn_records(case, turn_records)
        elif "response" in entry:
            record = grade_response(
                case,
                entry["response"],
                entry.get("wall_s", 0.0),
                entry.get("usage") or {},
            )
        else:
            continue
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


WORST_FIRST = ("error", "fail", "warn", "pass", "skip")


def merge_turn_records(
    case: dict[str, Any], turn_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Collapse a multi-turn case's per-turn records into one. Pure.

    Status is the worst turn: an agent that answers turn 1 correctly and then
    loses the thread on the follow-up has failed the conversation, and a mean
    would hide exactly the behaviour a multi-turn case exists to find.
    """

    statuses = [r["status"] for r in turn_records]
    worst = next((s for s in WORST_FIRST if s in statuses), "pass")
    failures: list[str] = []
    for index, record in enumerate(turn_records, start=1):
        for failure in record.get("failures") or []:
            failures.append(f"turn {index}: {failure}")
    return {
        "id": case["id"],
        "category": case["category"],
        "status": worst,
        "failures": failures,
        "soft": {"turns": [r.get("soft") or {} for r in turn_records]},
        "metrics": {
            "wall_s": round(
                sum((r.get("metrics") or {}).get("wall_s") or 0 for r in turn_records), 2
            ),
            "iterations": sum(
                (r.get("metrics") or {}).get("iterations") or 0 for r in turn_records
            ),
            "llm_calls": sum(
                (r.get("metrics") or {}).get("llm_calls") or 0 for r in turn_records
            ),
            "prompt_tokens": sum(
                (r.get("metrics") or {}).get("prompt_tokens") or 0 for r in turn_records
            ),
            "completion_tokens": sum(
                (r.get("metrics") or {}).get("completion_tokens") or 0
                for r in turn_records
            ),
            "tools_called": [
                name
                for r in turn_records
                for name in ((r.get("metrics") or {}).get("tools_called") or [])
            ],
            "tool_errors": sum(
                (r.get("metrics") or {}).get("tool_errors") or 0 for r in turn_records
            ),
            "turn_statuses": statuses,
        },
        "answer": turn_records[-1].get("answer") if turn_records else "",
        "turn_results": turn_records,
    }


def case_turns(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise single-prompt and multi-turn cases to one shape. Pure."""

    if case.get("turns"):
        return list(case["turns"])
    return [{"prompt": case["prompt"], "expect": case.get("expect", {})}]


def run_model_case(
    case: dict[str, Any],
    base_url: str,
    model: str,
    max_iterations: int,
    sampling: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one case, single-turn or multi-turn.

    Returns (graded record, raw record for the JSONL). The conversation is
    threaded through `history`, so a follow-up turn sees what the agent said
    before -- which is the point: "and what about the other one?" is only a
    test if the agent has to resolve the referent itself.
    """

    from urban_dossier_analyst.agent_loop import run_agent

    turns = case_turns(case)
    history: list[dict[str, Any]] = []
    turn_records: list[dict[str, Any]] = []
    raw_turns: list[dict[str, Any]] = []

    with injected_fault(case.get("fault_injection")):
        for index, turn in enumerate(turns, start=1):
            tracker = _UsageTracker()
            started = time.monotonic()
            response = run_agent(
                user_message=turn["prompt"],
                history=list(history) or None,
                max_iterations=max_iterations,
                vllm_base_url=f"{base_url}/v1",
                model=model,
                client_factory=tracker.factory,
                sampling=sampling,
            )
            wall = time.monotonic() - started
            usage = {
                "llm_calls": tracker.llm_calls,
                "prompt_tokens": tracker.prompt_tokens,
                "completion_tokens": tracker.completion_tokens,
            }
            turn_case = {**case, "prompt": turn["prompt"], "expect": turn.get("expect", {})}
            record = grade_response(turn_case, response, wall, usage)
            record["turn"] = index
            turn_records.append(record)
            raw_turns.append(
                {"turn": index, "prompt": turn["prompt"], "wall_s": round(wall, 2),
                 "usage": usage, "response": response}
            )
            history.append({"role": "user", "content": turn["prompt"]})
            history.append(
                {"role": "assistant", "content": response.get("answer") or ""}
            )

    if len(turn_records) == 1:
        record = turn_records[0]
        record.pop("turn", None)
        raw = {
            "case_id": case["id"],
            "wall_s": raw_turns[0]["wall_s"],
            "usage": raw_turns[0]["usage"],
            "response": raw_turns[0]["response"],
        }
        return record, raw

    merged = merge_turn_records(case, turn_records)
    raw = {"case_id": case["id"], "wall_s": merged["metrics"]["wall_s"],
           "usage": {k: merged["metrics"][k] for k in
                     ("llm_calls", "prompt_tokens", "completion_tokens")},
           "turns": raw_turns}
    return merged, raw


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
             "JSONL. Defaults to <output>.responses.jsonl. Replayable with "
             "--regrade after a grader change.",
    )
    parser.add_argument(
        "--no-responses", action="store_true",
        help="do not persist raw responses. Off by default: every stored "
             "comparison from before 2026-08-14 kept only the graded verdict, "
             "so nothing could be re-examined or re-graded afterwards.",
    )
    parser.add_argument(
        "--sampling", action="append", default=None, metavar="NAME=PROFILE",
        help="sampling profile per endpoint. PROFILE is a built-in name "
             f"({', '.join(sorted(SAMPLING_PROFILES))}), inline JSON, or "
             "@file.json. Use NAME=* to apply one profile to every endpoint. "
             "Recorded in the report.",
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

    # NAME=PROFILE, with "*" as the wildcard endpoint name.
    sampling_specs: dict[str, str] = {}
    for item in args.sampling or []:
        name, _, spec = item.partition("=")
        if not spec:
            parser.error(f"--sampling needs NAME=PROFILE, got {item}")
        sampling_specs[name.strip()] = spec.strip()
    try:
        sampling_by_endpoint = {
            name: resolve_sampling_spec(spec) for name, spec in sampling_specs.items()
        }
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        parser.error(f"--sampling: {exc}")

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
        "harness_version": HARNESS_VERSION,
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

    # Persist the trajectory unless explicitly told not to. Opting in was the
    # wrong default: the flag existed for the whole 2026-08-14 comparison and
    # was never passed, so every artifact from that day holds a verdict with
    # no way to see what the model actually did.
    responses_target = args.responses
    if responses_target is None and not args.no_responses and args.output:
        responses_target = str(Path(args.output).with_suffix(".responses.jsonl"))

    responses_fh = None
    if responses_target and not args.no_responses:
        responses_path = Path(responses_target)
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
        sampling = sampling_by_endpoint.get(name, sampling_by_endpoint.get("*", {}))
        sampling_label = sampling_specs.get(
            name, sampling_specs.get("*", "production")
        )
        print(
            f"[{name}] {model} at {url}: {len(model_cases)} cases "
            f"(sampling={sampling_label})"
        )

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
                        case, url, model, args.max_iterations, sampling
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
            # Which knobs produced these numbers. A comparison that does not
            # record this cannot be reproduced and cannot be defended.
            "sampling_profile": sampling_label,
            "sampling": sampling,
            "summary": summary,
            "results": results,
        }

    if responses_fh is not None:
        responses_fh.close()
        print(f"responses written to {responses_target}")

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
