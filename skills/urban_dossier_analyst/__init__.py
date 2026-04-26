"""urban-dossier-analyst skill package.

Master ReAct agent for goal-driven NYC neighborhood analysis. Runs on
DGX Spark against a local vLLM server hosting Nemotron-3-Nano-30B-A3B-NVFP4
with the qwen3_coder tool-call parser and the nano_v3 reasoning parser.
"""

from __future__ import annotations

__all__ = [
    "TOOLS",
    "dispatch_tool",
    "run_agent",
    "AgentResponse",
    "ToolCallTrace",
    "Point",
]

from .schemas import AgentResponse, Point, ToolCallTrace
from .tools import TOOLS, dispatch_tool
from .agent_loop import run_agent
