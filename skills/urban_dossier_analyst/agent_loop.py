"""ReAct loop for the urban-dossier-analyst agent.

Drives Nemotron-3-Nano-30B-A3B-NVFP4 served by vLLM (OpenAI-compatible API)
through a Thought -> Action -> Observation cycle, dispatching tool calls via
tools.dispatch_tool and feeding results back into the conversation.

Public surface:
  run_agent(user_message, history=None, max_iterations=8, reflection_every=3,
            vllm_base_url="http://localhost:8000/v1",
            model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4") -> dict

The returned dict matches schemas.AgentResponse:
  {answer, evidence, tools_called, iterations, trace}

Termination conditions:
  1. Model returns a final text answer with no tool calls.
  2. max_iterations is reached -> force a final-answer wrap-up.
  3. The same (tool_name, args) hash appears 3 times in a row -> abort.

Reflection:
  Every `reflection_every` iterations, append REFLECTION_PROMPT as a system
  message to force self-evaluation.

Test seam:
  Pass `client_factory` to inject a stub OpenAI client; tests/test_smoke.py
  uses this seam to validate termination without spinning up a vLLM server.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from .prompts import FINAL_ANSWER_PROMPT, REFLECTION_PROMPT, SYSTEM_PROMPT
from .schemas import AgentResponse, ToolCallTrace
from .tools import TOOLS, dispatch_tool


Message = dict[str, Any]
ClientFactory = Callable[[str], Any]


def _default_client_factory(base_url: str) -> Any:
    """Construct an OpenAI client pointed at the local vLLM server.

    Imported lazily so the package can be inspected without the openai SDK
    installed at module-load time. vLLM ignores the api_key but the SDK
    requires a non-empty value.
    """

    from openai import OpenAI  # type: ignore[import-not-found]

    return OpenAI(base_url=base_url, api_key="vllm-no-auth")


def _hash_tool_call(name: str, args: dict[str, Any]) -> str:
    """Stable hash of (tool_name, args) for repeated-call detection."""

    payload = json.dumps({"n": name, "a": args}, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort conversion of an OpenAI SDK / pydantic message to a dict."""

    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - try the next strategy
                continue
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {"value": str(obj)}


def _extract_evidence_from_trace(trace: list[ToolCallTrace]) -> list[dict[str, Any]]:
    """Derive a minimal evidence list from successful tool calls."""

    evidence: list[dict[str, Any]] = []
    for entry in trace:
        if "error" in entry.result:
            continue
        evidence.append(
            {
                "source": entry.tool_name,
                "detail": _summarize_result(entry.tool_name, entry.result),
            }
        )
    return evidence


def _summarize_result(tool_name: str, result: dict[str, Any]) -> str:
    """One-line summary of a tool result for the evidence list."""

    if tool_name == "search_address":
        hits = result.get("results", [])
        return f"{len(hits)} address candidate(s) returned"
    if tool_name == "score_neighborhood":
        scores = result.get("scores", {})
        return f"scores: {sorted(scores.keys())}" if scores else "score payload returned"
    if tool_name == "find_similar_neighborhoods":
        return f"{len(result.get('neighbors', []))} neighbor(s) returned"
    if tool_name == "query_dataset":
        return f"{result.get('total', 0)} row(s) for dataset_id={result.get('dataset_id')}"
    if tool_name == "retrieve_dataset_docs":
        return f"{len(result.get('hits', []))} doc snippet(s)"
    return "tool result captured"


def run_agent(
    user_message: str,
    history: list[dict] | None = None,
    max_iterations: int = 8,
    reflection_every: int = 3,
    vllm_base_url: str = "http://localhost:8000/v1",
    model: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-NVFP4",
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Run the ReAct loop and return the structured agent response.

    Args:
      user_message:    The user's free-text question.
      history:         Prior turns (OpenAI-style messages). Optional.
      max_iterations:  Hard cap on Thought-Action-Observation cycles.
      reflection_every: Inject REFLECTION_PROMPT every N iterations.
      vllm_base_url:   OpenAI-compatible base URL of the local vLLM server.
      model:           Model name registered with vLLM at startup.
      client_factory:  Test seam. Defaults to the real openai.OpenAI client.

    Returns:
      Dict matching schemas.AgentResponse.
    """

    if client_factory is None:
        client_factory = _default_client_factory
    client = client_factory(vllm_base_url)

    messages: list[Message] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    trace: list[ToolCallTrace] = []
    tools_called: list[str] = []
    recent_hashes: list[str] = []
    iterations = 0
    final_answer: str = ""

    for iteration in range(max_iterations):
        iterations = iteration + 1

        # Reflection injection (skip iteration 0).
        if iteration > 0 and iteration % reflection_every == 0:
            messages.append({"role": "system", "content": REFLECTION_PROMPT})

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001 - surface upstream failures
            final_answer = (
                f"Agent loop aborted: vLLM call failed at iteration "
                f"{iterations} with {type(exc).__name__}: {exc}"
            )
            break

        choice = response.choices[0]
        msg = choice.message
        msg_dict = _to_dict(msg)
        tool_calls = msg_dict.get("tool_calls") or []
        content = msg_dict.get("content") or ""

        # The OpenAI SDK requires the assistant message echoed back into
        # history before any tool messages are appended.
        assistant_msg: Message = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # Termination: model produced text and no tool calls.
        if not tool_calls:
            final_answer = content.strip()
            break

        # Dispatch each requested tool call.
        for call in tool_calls:
            call_dict = _to_dict(call)
            call_id = call_dict.get("id") or f"call_{iteration}"
            fn_block = call_dict.get("function") or {}
            tool_name = fn_block.get("name", "")
            raw_args = fn_block.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except json.JSONDecodeError:
                args = {}

            # Repeated-call detection.
            call_hash = _hash_tool_call(tool_name, args)
            recent_hashes.append(call_hash)
            recent_hashes = recent_hashes[-3:]
            if len(recent_hashes) == 3 and len(set(recent_hashes)) == 1:
                final_answer = (
                    f"Agent loop aborted at iteration {iterations}: tool "
                    f"'{tool_name}' was called with identical arguments three "
                    f"times in a row. The model is stuck in a loop."
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "content": json.dumps(
                            {"error": "repeated_call_abort", "hash": call_hash}
                        ),
                    }
                )
                trace.append(
                    ToolCallTrace(
                        iteration=iteration,
                        tool_name=tool_name,
                        args=args,
                        result={"error": "repeated_call_abort"},
                        latency_ms=0,
                    )
                )
                tools_called.append(tool_name)
                return AgentResponse(
                    answer=final_answer,
                    evidence=_extract_evidence_from_trace(trace),
                    tools_called=tools_called,
                    iterations=iterations,
                    trace=trace,
                ).model_dump()

            # Normal dispatch path.
            started = time.perf_counter()
            result = dispatch_tool(tool_name, args)
            latency_ms = int((time.perf_counter() - started) * 1000)

            tools_called.append(tool_name)
            trace.append(
                ToolCallTrace(
                    iteration=iteration,
                    tool_name=tool_name,
                    args=args,
                    result=result,
                    latency_ms=latency_ms,
                )
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": json.dumps(result, default=str),
                }
            )
    else:
        # Loop ran to max_iterations without a final answer. Force the model
        # to produce one with FINAL_ANSWER_PROMPT.
        messages.append({"role": "system", "content": FINAL_ANSWER_PROMPT})
        try:
            wrapup = client.chat.completions.create(
                model=model,
                messages=messages,
                tool_choice="none",
                temperature=0.2,
            )
            final_answer = (_to_dict(wrapup.choices[0].message).get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            final_answer = (
                f"Agent loop hit max_iterations={max_iterations} and the "
                f"final-answer wrap-up call failed with "
                f"{type(exc).__name__}: {exc}"
            )

    if not final_answer:
        final_answer = (
            "Agent loop terminated without producing a final answer. "
            "Inspect the trace for missing tools or upstream errors."
        )

    return AgentResponse(
        answer=final_answer,
        evidence=_extract_evidence_from_trace(trace),
        tools_called=tools_called,
        iterations=iterations,
        trace=trace,
    ).model_dump()
