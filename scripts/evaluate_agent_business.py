#!/usr/bin/env python3
"""Validate and replay the fixed Urban Dossier Agent business evaluation.

The runner deliberately separates collection from grading. A live run writes
one JSON object per case; the same artifact can later be regraded after scorer
changes without spending another model run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "agent" / "business_cases.json"
VALID_INTENTS = {"ask_from_evidence", "new_analysis", "meta_help", "out_of_scope"}
VALID_TOOLS = {
    "score_neighborhood",
    "compare_neighborhoods",
    "query_dataset",
    "find_similar_neighborhoods",
    "walking_isochrone",
    "simulate_intervention",
    "search_address",
    "retrieve_dataset_docs",
}


def load_corpus(path: Path) -> dict[str, Any]:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    return corpus


def validate_corpus(corpus: dict[str, Any]) -> None:
    if corpus.get("schema_version") != "1.0":
        raise ValueError("business corpus schema_version must be '1.0'")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or not 20 <= len(cases) <= 30:
        raise ValueError("business corpus must contain 20 to 30 cases")

    ids: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{prefix} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{prefix}.id must be a non-empty string")
        if case_id in ids:
            raise ValueError(f"duplicate case id: {case_id}")
        ids.add(case_id)
        if case.get("intent") not in VALID_INTENTS:
            raise ValueError(f"{case_id}: invalid intent {case.get('intent')!r}")
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ValueError(f"{case_id}: prompt must be non-empty")
        if not isinstance(case.get("evidence_required"), bool):
            raise ValueError(f"{case_id}: evidence_required must be boolean")
        for field in ("all_tools", "ordered_tools", "forbidden_tools"):
            tools = case.get(field, [])
            if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
                raise ValueError(f"{case_id}: {field} must be a string list")
            unknown = set(tools) - VALID_TOOLS
            if unknown:
                raise ValueError(f"{case_id}: {field} has unknown tools: {sorted(unknown)}")
        terms = case.get("answer_terms_any", [])
        if not isinstance(terms, list) or not all(isinstance(item, str) and item for item in terms):
            raise ValueError(f"{case_id}: answer_terms_any must be a non-empty-string list")


def corpus_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool_names(response: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in response.get("tools_called", []) or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.append(name)
    if names:
        return names
    for item in response.get("trace", []) or []:
        if isinstance(item, dict) and isinstance(item.get("tool_name"), str):
            names.append(item["tool_name"])
    return names


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    position = 0
    for item in actual:
        if position < len(expected) and item == expected[position]:
            position += 1
    return position == len(expected)


def grade_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    answer = str(response.get("answer", "")).strip()
    tools = _tool_names(response)
    checks: dict[str, bool] = {"answer_present": bool(answer)}

    if "all_tools" in case:
        expected = case["all_tools"]
        checks["required_tools"] = all(tool in tools for tool in expected)
        if not expected:
            checks["no_tools"] = not tools
    if case.get("ordered_tools"):
        checks["ordered_tools"] = _is_subsequence(case["ordered_tools"], tools)
    if case.get("forbidden_tools"):
        checks["forbidden_tools"] = not (set(case["forbidden_tools"]) & set(tools))
    if case["evidence_required"]:
        checks["evidence_present"] = bool(response.get("evidence"))
    if case.get("answer_terms_any"):
        lowered = answer.casefold()
        checks["answer_terms_any"] = any(
            term.casefold() in lowered for term in case["answer_terms_any"]
        )

    return {
        "case_id": case["id"],
        "intent": case["intent"],
        "release_gate": case.get("release_gate"),
        "passed": all(checks.values()),
        "checks": checks,
        "tools_called": tools,
    }


def _post_case(base_url: str, case: dict[str, Any], token: str | None, timeout: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/agent/ask"
    body = json.dumps({"message": case["prompt"]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            payload = json.loads(result.read().decode("utf-8"))
            status = result.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"detail": raw}
        status = exc.code
    return {
        "case_id": case["id"],
        "http_status": status,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "response": payload,
    }


def collect_live(
    corpus: dict[str, Any], base_url: str, token: str | None, timeout: float
) -> list[dict[str, Any]]:
    rows = []
    for number, case in enumerate(corpus["cases"], start=1):
        print(f"[{number:02d}/{len(corpus['cases'])}] {case['id']}", file=sys.stderr)
        try:
            rows.append(_post_case(base_url, case, token, timeout))
        except (OSError, ValueError) as exc:
            rows.append({"case_id": case["id"], "error": str(exc), "response": {}})
    return rows


def load_responses(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str):
            raise ValueError(f"{path}:{line_number}: expected object with case_id")
        if "response" not in row:
            row = {"case_id": row["case_id"], "response": row}
        rows.append(row)
    return rows


def build_report(
    corpus: dict[str, Any], rows: list[dict[str, Any]], cases_path: Path
) -> dict[str, Any]:
    by_id = {row["case_id"]: row for row in rows}
    results = []
    for case in corpus["cases"]:
        row = by_id.get(case["id"], {"response": {}})
        grade = grade_case(case, row.get("response") or {})
        if row.get("error") or (row.get("http_status") not in (None, 200)):
            grade["passed"] = False
            grade["transport_error"] = row.get("error") or f"HTTP {row.get('http_status')}"
        grade["latency_ms"] = row.get("latency_ms")
        results.append(grade)

    intent_counts = Counter(item["intent"] for item in results)
    intent_passes = Counter(item["intent"] for item in results if item["passed"])
    passed = sum(item["passed"] for item in results)
    return {
        "schema_version": "1.0",
        "corpus_sha256": corpus_sha256(cases_path),
        "summary": {
            "passed": passed,
            "total": len(results),
            "pass_rate": round(passed / len(results), 4),
        },
        "by_intent": {
            intent: {"passed": intent_passes[intent], "total": intent_counts[intent]}
            for intent in sorted(intent_counts)
        },
        "results": results,
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base-url", help="Live service root, for example http://127.0.0.1:8001")
    source.add_argument("--responses", type=Path, help="Previously collected JSONL responses")
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "results" / "agent-business.jsonl")
    parser.add_argument("--token")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    corpus = load_corpus(args.cases)
    if args.validate_only:
        print(f"valid: {len(corpus['cases'])} cases, sha256={corpus_sha256(args.cases)}")
        return 0
    if not args.base_url and not args.responses:
        raise SystemExit("choose --base-url for a live run or --responses for replay")

    if args.responses:
        rows = load_responses(args.responses)
    else:
        rows = collect_live(corpus, args.base_url, args.token, args.timeout)
        write_jsonl(args.output, rows)

    report = build_report(corpus, rows, args.cases)
    report_path = args.output.with_suffix(".report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"report: {report_path}")
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
