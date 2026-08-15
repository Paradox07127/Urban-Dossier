"""Shared Pydantic schemas used by tools.py and agent_loop.py.

These models define the structured output of the agent and the trace records
captured during the ReAct loop. Keeping them in a single module avoids
circular imports between the tool layer and the loop layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# NYC bounding box - reused for argument validation in tools.py.
NYC_LAT_MIN: float = 40.4
NYC_LAT_MAX: float = 40.95
NYC_LON_MIN: float = -74.3
NYC_LON_MAX: float = -73.7


class Point(BaseModel):
    """A latitude/longitude pair clamped to the NYC bounding box."""

    latitude: float = Field(ge=NYC_LAT_MIN, le=NYC_LAT_MAX)
    longitude: float = Field(ge=NYC_LON_MIN, le=NYC_LON_MAX)


class ToolCallTrace(BaseModel):
    """A single Thought-Action-Observation triple captured for debugging.

    The agent loop appends one of these per dispatched tool call. The
    frontend can render the trace as a timeline; tests use it to assert
    termination behavior.
    """

    iteration: int = Field(ge=0, description="0-based iteration index")
    tool_name: str
    args: dict[str, Any]
    result: dict[str, Any]
    latency_ms: int = Field(ge=0)


class TurnTrace(BaseModel):
    """What the model produced on one iteration, before any tool ran.

    ToolCallTrace records what the agent DID; this records what it was
    thinking when it decided to. Reasoning models put their chain of thought
    in a separate channel that the loop otherwise only reads as a fallback
    for a missing answer, so without this the deliberation is never captured
    anywhere -- and a model-selection decision made from tool names alone
    cannot explain itself after the fact.
    """

    iteration: int = Field(ge=0, description="0-based iteration index")
    reasoning: str = ""
    content: str = ""
    finish_reason: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    kind: str = Field(
        default="loop",
        description="loop | wrapup_truncated | wrapup_max_iterations "
                    "| wrapup_no_progress",
    )


class AgentResponse(BaseModel):
    """The structured payload returned by run_agent.

    Fields:
      answer:       Free-text final answer destined for the user.
      evidence:     Per-claim citations of which dataset / tool produced the
                    underlying number. Each entry is a small dict with at
                    least {"source": str, "detail": str}.
      tools_called: Names of tools called, in dispatch order. May contain
                    duplicates - useful for usage analytics.
      iterations:   Number of ReAct iterations consumed (>= 1).
      trace:        Full ToolCallTrace list. Empty when the model answered
                    on iteration 0 with no tool calls.
      turns:        One TurnTrace per model call, including the wrap-up
                    calls. This is the deliberation record; `trace` is the
                    action record.
    """

    answer: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    iterations: int = Field(ge=0)
    trace: list[ToolCallTrace] = Field(default_factory=list)
    turns: list[TurnTrace] = Field(default_factory=list)
