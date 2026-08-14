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
  Every `reflection_every` iterations, append REFLECTION_PROMPT to force
  self-evaluation.  Only the leading message carries role="system"; every
  mid-conversation directive goes in as role="user" via _append_directive,
  because some chat templates (Qwen3.8's) reject a later system message
  outright.

Test seam:
  Pass `client_factory` to inject a stub OpenAI client; tests/test_smoke.py
  uses this seam to validate termination without spinning up a vLLM server.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable

from .prompts import FINAL_ANSWER_PROMPT, REFLECTION_PROMPT, SYSTEM_PROMPT
from .schemas import AgentResponse, ToolCallTrace, TurnTrace
from .tools import (
    dispatch_tool,
    get_available_tools,
    tool_availability,
    tool_availability_prompt,
)


Message = dict[str, Any]
ClientFactory = Callable[[str], Any]

# 0.2 is the production setting for every model this service has served, and
# stays the default -- these knobs exist so a benchmark can ask "is this
# candidate being penalised by our sampling?" without editing the loop.
# Qwen3.8, for one, asks for 1.0 with thinking on and 0.7 with it off, well
# away from what the Nemotron checkpoints were tuned against here.
# _WRAPUP_TEMPERATURE applies to the two thinking-disabled wrap-up calls,
# which is the "instruct mode" half of that split.
_TEMPERATURE = float(os.environ.get("URBAN_DOSSIER_AGENT_TEMPERATURE", "0.2"))
_WRAPUP_TEMPERATURE = float(
    os.environ.get("URBAN_DOSSIER_AGENT_WRAPUP_TEMPERATURE", "0.2")
)


def _turn(
    iteration: int,
    msg_dict: dict[str, Any],
    finish_reason: Any,
    tool_calls: list[Any],
    kind: str,
) -> TurnTrace:
    """Capture one model turn: what it thought, said, and decided to call."""

    names: list[str] = []
    for call in tool_calls or []:
        fn = (call or {}).get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))

    reasoning = ""
    for key in ("reasoning", "reasoning_content"):
        value = msg_dict.get(key)
        if isinstance(value, str) and value.strip():
            reasoning = value.strip()
            break

    raw_content = msg_dict.get("content")
    return TurnTrace(
        iteration=iteration,
        reasoning=reasoning,
        content=raw_content.strip() if isinstance(raw_content, str) else "",
        finish_reason=str(finish_reason or ""),
        tool_calls=names,
        kind=kind,
    )


def _append_directive(messages: list[Message], text: str) -> None:
    """Inject a mid-conversation steering message (reflection, final-answer).

    These used to go in as role="system", which the Nemotron checkpoints
    accept anywhere in the conversation.  Qwen3.8's chat template does not:
    it rejects the whole request with HTTP 400 "System message must be at the
    beginning", killing the loop mid-run.  Measured 2026-08-14, that took out
    3 of Qwen3.8's 20 business-eval cases -- and they were scored as model
    failures, which they were not.

    role="user" is accepted by every chat template we serve and reads the
    same: both directives already announce themselves with a bracketed
    [header], so they do not pass as something the human typed.
    """

    messages.append({"role": "user", "content": text})


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


def _message_text(msg_dict: dict[str, Any]) -> str:
    """Return the model's user-facing text.

    Nemotron is a reasoning model: vLLM puts the chain of thought in
    ``reasoning`` (older builds: ``reasoning_content``) and the answer in
    ``content``. When a response is cut off mid-reasoning ``content`` is None,
    so falling back to the reasoning text is the difference between surfacing a
    partial answer and returning nothing at all.
    """

    for key in ("content", "reasoning", "reasoning_content"):
        value = msg_dict.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _compact_observation(result: dict[str, Any], budget_chars: int) -> str:
    """Serialize a tool result for the model under a context budget.

    ``score_neighborhood`` returns the full detail payload -- about 59k chars
    (~15k tokens) for a single point, half of a 32k context window. Feeding
    that back verbatim left so little room that the model exhausted its budget
    mid-reasoning and returned a truncated response with empty content.

    Large top-level fields are dropped biggest-first until the payload fits.
    The dropped names are reported back to the model so it can re-query for a
    specific slice instead of silently reasoning over a hole. The trace and the
    API response keep the untouched result, so the UI loses nothing.
    """

    encoded = json.dumps(result, default=str)
    if len(encoded) <= budget_chars:
        return encoded

    kept = dict(result)
    omitted: list[str] = []
    # Never drop the small fields that carry the actual answer.
    protected = {"scores", "target", "error", "retry_hint", "latency_ms"}

    by_size = sorted(
        ((k, len(json.dumps(v, default=str))) for k, v in kept.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    for key, _size in by_size:
        if len(json.dumps(kept, default=str)) <= budget_chars:
            break
        if key in protected:
            continue
        kept.pop(key, None)
        omitted.append(key)

    if omitted:
        kept["_omitted_fields"] = omitted
        kept["_omitted_note"] = (
            "Large fields were removed to fit the context window. Call "
            "query_dataset for a specific slice if you need them."
        )
    return json.dumps(kept, default=str)


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
    observation_budget_chars: int = 8000,
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
      observation_budget_chars: Max serialized size of a single tool result fed
                       back to the model. Oversized results are reduced by
                       dropping their largest fields; the full result is still
                       recorded in the returned trace.

    Returns:
      Dict matching schemas.AgentResponse.
    """

    if client_factory is None:
        client_factory = _default_client_factory
    client = client_factory(vllm_base_url)

    availability = tool_availability()
    active_tools = get_available_tools(availability)
    active_tool_names = {tool["function"]["name"] for tool in active_tools}
    messages: list[Message] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + tool_availability_prompt(availability),
        }
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    trace: list[ToolCallTrace] = []
    turns: list[TurnTrace] = []
    tools_called: list[str] = []
    recent_hashes: list[str] = []
    iterations = 0
    final_answer: str = ""

    for iteration in range(max_iterations):
        iterations = iteration + 1

        # Reflection injection (skip iteration 0).
        if iteration > 0 and iteration % reflection_every == 0:
            _append_directive(messages, REFLECTION_PROMPT)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=active_tools,
                tool_choice="auto",
                temperature=_TEMPERATURE,
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
        # Keep these separate: ``content`` is a real user-facing answer, while
        # ``fallback_text`` may be raw chain of thought. Reasoning is a last
        # resort only -- never the preferred answer.
        raw_content = msg_dict.get("content")
        content = raw_content.strip() if isinstance(raw_content, str) else ""
        fallback_text = _message_text(msg_dict)
        finish_reason = getattr(choice, "finish_reason", None)
        turns.append(
            _turn(iteration, msg_dict, finish_reason, tool_calls, kind="loop")
        )

        # The OpenAI SDK requires the assistant message echoed back into
        # history before any tool messages are appended.
        assistant_msg: Message = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # Termination: model produced text and no tool calls.
        #
        # ``finish_reason == "length"`` means the response was cut off, not
        # that the model was done. Treating that as a final answer is what made
        # a truncated reasoning block look like a completed-but-empty turn, so
        # ask for a bounded wrap-up instead of terminating on the fragment.
        if not tool_calls:
            if content:
                final_answer = content
            elif finish_reason == "length":
                # Cut off before it produced an answer. Ask for a short one
                # rather than handing the user raw chain of thought.
                _append_directive(messages, FINAL_ANSWER_PROMPT)
                try:
                    wrapup = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tool_choice="none",
                        temperature=_WRAPUP_TEMPERATURE,
                        max_tokens=1024,
                        # Wrap-up wants a direct answer; with thinking on,
                        # reasoning models (Nemotron 3.5 measured at ~1K
                        # thinking tokens) can exhaust this budget inside the
                        # think block and return empty content.
                        extra_body={
                            "chat_template_kwargs": {"enable_thinking": False}
                        },
                    )
                    wrap_choice = wrapup.choices[0]
                    wrap_dict = _to_dict(wrap_choice.message)
                    turns.append(
                        _turn(
                            iteration,
                            wrap_dict,
                            getattr(wrap_choice, "finish_reason", None),
                            [],
                            kind="wrapup_truncated",
                        )
                    )
                    final_answer = _message_text(wrap_dict)
                except Exception as exc:  # noqa: BLE001
                    final_answer = (
                        f"Agent loop hit the model's token limit at iteration "
                        f"{iterations} and the wrap-up call failed with "
                        f"{type(exc).__name__}: {exc}"
                    )
                if not final_answer:
                    final_answer = fallback_text
            else:
                final_answer = fallback_text
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
                    turns=turns,
                ).model_dump()

            # Refuse a forged or stale call even if the model emits a tool that
            # was not present in this request's published schema list.
            started = time.perf_counter()
            if tool_name not in active_tool_names:
                state = availability.get(tool_name, {})
                result = {
                    "error": "tool_not_released",
                    "tool": tool_name,
                    "reason": state.get("reason", "unknown_tool"),
                    "retry_hint": "Use only tools published for this request.",
                }
            else:
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
                    "content": _compact_observation(result, observation_budget_chars),
                }
            )
    else:
        # Loop ran to max_iterations without a final answer. Force the model
        # to produce one with FINAL_ANSWER_PROMPT.
        _append_directive(messages, FINAL_ANSWER_PROMPT)
        try:
            wrapup = client.chat.completions.create(
                model=model,
                messages=messages,
                tool_choice="none",
                temperature=_WRAPUP_TEMPERATURE,
                max_tokens=1024,
                # Same rationale as the truncation wrap-up above: direct
                # answer only, no think block eating the 1024 budget.
                extra_body={
                    "chat_template_kwargs": {"enable_thinking": False}
                },
            )
            wrap_choice = wrapup.choices[0]
            wrap_dict = _to_dict(wrap_choice.message)
            turns.append(
                _turn(
                    iterations,
                    wrap_dict,
                    getattr(wrap_choice, "finish_reason", None),
                    [],
                    kind="wrapup_max_iterations",
                )
            )
            final_answer = _message_text(wrap_dict)
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
        turns=turns,
    ).model_dump()
