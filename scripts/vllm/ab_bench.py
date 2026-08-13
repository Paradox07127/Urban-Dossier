#!/usr/bin/env python3
"""A/B benchmark for OpenAI-compatible vLLM endpoints.

Measures, per endpoint: TTFT (p50/p95), output throughput, and wall time, at
each requested concurrency level, using streaming chat completions.  Captures
every completion verbatim so quality can be reviewed side by side afterwards.

Stdlib only — runs from any Python 3.10+ without installing anything.

Usage:
    python3 scripts/vllm/ab_bench.py \
        --endpoint current=http://127.0.0.1:8000 \
        --endpoint lightning=http://127.0.0.1:8002 \
        --concurrency 1 --concurrency 4 \
        --max-tokens 512 \
        --output /tmp/ab_bench.json

The prompt set is Urban-Dossier-flavoured: the same scoring-rubric system
prompt the backend sends on every /api/analyze-point, plus mixed analytical
questions, so prefix caching and reasoning behave as they do in production.

Exit code 0 means the run is fit to compare: every endpoint answered, every
request succeeded, every prompt completed. Exit 1 means it is not, and says
why on stderr -- the report is still written either way, because partial
numbers are worth reading even when they are not worth deciding on.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "You are the Urban Dossier analyst. Score neighborhoods from evidence "
    "tables only; cite dataset names for every claim; never invent numbers. "
    "When a question cannot be answered from the provided evidence, say so."
)

PROMPTS = [
    "A block in Mott Haven scores 34 on amenities and 71 on transit. Draft a "
    "three-sentence assessment for a resident deciding whether to move there.",
    "Explain, step by step, how you would decide whether a jump from 205 to "
    "260 noise complaints in one ZIP is seasonal or structural. List the "
    "datasets you would consult.",
    "Compare two candidate sites for a new library: site A (walk score 88, "
    "flood zone AE) and site B (walk score 61, no flood risk). Recommend one "
    "and state the deciding factor.",
    "Summarize in one paragraph what a composite 'family friendliness' score "
    "should include, and name one metric that should NOT be included.",
    "A council member asks why their district's safety score fell 6 points "
    "after a methodology update that they did not vote on. Write a careful, "
    "non-defensive two-paragraph reply.",
    "Given monthly 311 rodent complaints [12, 9, 14, 41, 38, 44], is this a "
    "level shift or a trend? Answer with your reasoning.",
    "List the top three failure modes of using complaint counts as a proxy "
    "for neighborhood quality, one sentence each.",
    "Write a JSON object with keys 'headline', 'evidence', 'caveat' "
    "summarizing: median rent up 11% YoY while new housing permits fell 30%.",
]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def get_model_id(base_url: str) -> str:
    with urllib.request.urlopen(f"{base_url}/v1/models", timeout=30) as resp:
        data = json.load(resp)
    return data["data"][0]["id"]


def one_request(base_url: str, model: str, prompt: str, max_tokens: int,
                timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 1.0,
        "top_p": 0.95,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    start = time.monotonic()
    ttft = None
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    completion_tokens = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            usage = event.get("usage")
            if usage:
                completion_tokens = usage.get("completion_tokens", 0)
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                piece = delta.get("content") or ""
                # vLLM <=0.23 streams thinking as `reasoning_content`;
                # 0.27 renamed the field to `reasoning`.
                thinking = (delta.get("reasoning_content")
                            or delta.get("reasoning") or "")
                if (piece or thinking) and ttft is None:
                    ttft = time.monotonic() - start
                if piece:
                    chunks.append(piece)
                if thinking:
                    reasoning_chunks.append(thinking)
    elapsed = time.monotonic() - start
    return {
        "prompt": prompt,
        "ttft_s": ttft,
        "elapsed_s": elapsed,
        "completion_tokens": completion_tokens,
        "content": "".join(chunks),
        "reasoning": "".join(reasoning_chunks),
    }


def run_level(base_url: str, model: str, concurrency: int, max_tokens: int,
              timeout: float) -> dict:
    started = time.monotonic()
    results: list[dict] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(one_request, base_url, model, prompt, max_tokens, timeout)
            for prompt in PROMPTS
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(str(exc))
    wall = time.monotonic() - started
    ttfts = [r["ttft_s"] for r in results if r["ttft_s"] is not None]
    total_tokens = sum(r["completion_tokens"] for r in results)
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "errors": errors,
        "wall_s": round(wall, 2),
        "ttft_p50_s": round(percentile(ttfts, 0.50), 3) if ttfts else None,
        "ttft_p95_s": round(percentile(ttfts, 0.95), 3) if ttfts else None,
        "output_tokens": total_tokens,
        "output_tok_per_s": round(total_tokens / wall, 1) if wall else None,
        "mean_completion_tokens": (
            round(statistics.mean(r["completion_tokens"] for r in results), 1)
            if results else None),
        "completions": results,
    }


def failure_reasons(report: dict, expected_requests: int) -> list[str]:
    """Everything that makes this run unfit to decide anything. Pure.

    A benchmark that always exits 0 cannot be a gate: an unreachable
    endpoint would read as "no problems found" to any script that trusts
    the status code. Three ways a run is untrustworthy, and all three are
    already visible in the report -- they just were not being counted.
    """
    reasons: list[str] = []
    for name, entry in (report.get("endpoints") or {}).items():
        if entry.get("error"):
            reasons.append(f"{name}: endpoint unreachable ({entry['error']})")
            continue
        for level in entry.get("levels") or []:
            errors = level.get("errors") or []
            if errors:
                reasons.append(
                    f"{name} C{level['concurrency']}: {len(errors)} request "
                    f"error(s), first: {errors[0]}"
                )
            # Silent partial results skew every per-second number below.
            if level.get("requests", 0) < expected_requests:
                reasons.append(
                    f"{name} C{level['concurrency']}: only "
                    f"{level.get('requests', 0)}/{expected_requests} requests "
                    "completed"
                )
    if not report.get("endpoints"):
        reasons.append("no endpoints were benchmarked")
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", action="append", required=True,
                        metavar="NAME=URL",
                        help="label and base URL, e.g. current=http://127.0.0.1:8000")
    parser.add_argument("--concurrency", action="append", type=int, default=None,
                        help="concurrency level, repeatable (default: 1 and 4)")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--output", default=None,
                        help="write the full JSON report here")
    args = parser.parse_args()
    levels = args.concurrency or [1, 4]

    report: dict = {"endpoints": {}, "generated_unix": int(time.time())}
    for spec in args.endpoint:
        name, _, url = spec.partition("=")
        url = url.rstrip("/")
        if not url:
            parser.error(f"--endpoint needs NAME=URL, got: {spec}")
        try:
            model = get_model_id(url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[{name}] UNREACHABLE at {url}: {exc}", file=sys.stderr)
            report["endpoints"][name] = {"url": url, "error": str(exc)}
            continue
        print(f"[{name}] {model} at {url}")
        entry = {"url": url, "model": model, "levels": []}
        for level in levels:
            summary = run_level(url, model, level, args.max_tokens, args.timeout)
            entry["levels"].append(summary)
            print(f"[{name}] C{level}: wall {summary['wall_s']}s, "
                  f"TTFT p50 {summary['ttft_p50_s']}s, "
                  f"{summary['output_tok_per_s']} tok/s, "
                  f"{len(summary['errors'])} errors")
        report["endpoints"][name] = entry

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"report written to {args.output}")

    # The report is still written on failure -- partial numbers are worth
    # reading, they are just not worth deciding on. The exit code is what
    # says so.
    reasons = failure_reasons(report, len(PROMPTS))
    for reason in reasons:
        print(f"FAIL {reason}", file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
