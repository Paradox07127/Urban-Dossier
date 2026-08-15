"""urban-dossier-analyst skill package.

Master ReAct agent for goal-driven NYC neighborhood analysis. Runs on
DGX Spark against a local vLLM server exposing an OpenAI-compatible API.
The served checkpoint is deployment configuration, not a property of this
package: production, Lightning and Qwen3.8 candidates all run this same
loop, and the prompts deliberately name no model.
"""

from __future__ import annotations

__all__ = [
    "TOOLS",
    "dispatch_tool",
    "get_available_tools",
    "tool_availability",
    "run_agent",
    "AgentResponse",
    "ToolCallTrace",
    "Point",
]

from .schemas import AgentResponse, Point, ToolCallTrace
from .tools import TOOLS, dispatch_tool, get_available_tools, tool_availability
from .agent_loop import run_agent
