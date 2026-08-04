"""OpenClaw Gateway transport for the ReAct loop.

Why this exists
---------------
``run_agent`` originally spoke OpenAI chat-completions directly to vLLM on
:8000, bypassing OpenShell entirely. The deployment rule is that OpenShell is
the policy/network isolation boundary for agent traffic, so the loop now goes
through the authenticated Gateway inside the sandbox instead.

The Gateway speaks OpenResponses, not chat-completions, and its implementation
has two constraints that shape everything below (both verified against the
running 2026.7.1 Gateway, not assumed):

  1. ``input`` accepts a plain string only. The array form used to replay a
     conversation -- including ``function_call_output`` items -- is rejected
     with "input: Invalid input". Tool results therefore go back as text.
  2. Conversation state lives on the server, keyed by the
     ``x-openclaw-session-key`` header. A fresh key has no memory, the same key
     continues the conversation. So each turn sends only what is *new*;
     resending the whole history would duplicate it.

Client-supplied function tools *are* supported: the Gateway returns
``function_call`` output items with ``call_id``/``name``/``arguments``, which is
what makes a tool-using loop possible over this transport at all.

This module presents a ``chat.completions.create`` surface so ``agent_loop``
does not need to know which transport it is on. The loop keeps its reasoning
extraction, truncation handling and observation budgeting unchanged.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any


DEFAULT_GATEWAY_URL = "http://127.0.0.1:18789"
DEFAULT_TOKEN_FILE = "/mnt/data/urban-dossier-state/runtime/openclaw-gateway.token"
DEFAULT_AGENT_ID = "urban-dossier"


class GatewayError(RuntimeError):
    """Raised when the Gateway cannot be reached or refuses the request."""


def read_gateway_token(token_file: str | None = None) -> str | None:
    """Read the bearer token from the environment or its mode-0600 file.

    The token must never be logged or echoed; callers only ever check whether
    it is present.
    """

    inline = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if inline:
        return inline
    path = token_file or os.environ.get(
        "OPENCLAW_GATEWAY_TOKEN_FILE", DEFAULT_TOKEN_FILE
    )
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip().strip('"') or None
    except OSError:
        return None


def _chat_tools_to_responses(tools: list[dict] | None) -> list[dict] | None:
    """Convert chat-completions tool schemas to the OpenResponses flat form.

    chat:      {"type": "function", "function": {"name", "description", "parameters"}}
    responses: {"type": "function", "name", "description", "parameters"}
    """

    if not tools:
        return None
    converted: list[dict] = []
    for tool in tools:
        fn = tool.get("function") or {}
        converted.append(
            {
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return converted


def _render_turn(messages: list[dict]) -> str:
    """Flatten the new messages of one turn into a single string input.

    Assistant messages are dropped: they are the Gateway's own prior output and
    it already has them server-side. Re-sending would double them up.
    """

    parts: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            parts.append(f"[Instructions]\n{content}")
        elif role == "user":
            parts.append(str(content))
        elif role == "tool":
            name = message.get("name", "tool")
            parts.append(
                f"[Tool result: {name}]\n{content}\n\n"
                "Use this result to continue. Call another tool only if you "
                "still need more evidence, otherwise write the final answer."
            )
    return "\n\n".join(part for part in parts if part.strip())


class _Message:
    """Chat-completions-shaped message built from OpenResponses output items."""

    def __init__(self, content: str | None, tool_calls: list[dict] | None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self) -> dict[str, Any]:
        return {"content": self.content, "tool_calls": self.tool_calls}


class _Choice:
    def __init__(self, message: _Message, finish_reason: str):
        self.message = message
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, choice: _Choice):
        self.choices = [choice]


class GatewayChatAdapter:
    """Speaks ``chat.completions.create`` on the outside, OpenResponses inside.

    One adapter instance corresponds to one agent run: it owns a session key
    and tracks how much of the conversation it has already sent.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        agent_id: str | None = None,
        session_key: str | None = None,
        token: str | None = None,
        timeout: float = 180.0,
        max_output_tokens: int = 4096,
        http_post: Any = None,
    ):
        self.base_url = (base_url or os.environ.get("OPENCLAW_GATEWAY_URL", DEFAULT_GATEWAY_URL)).rstrip("/")
        self.agent_id = agent_id or os.environ.get("OPENCLAW_AGENT_ID", DEFAULT_AGENT_ID)
        self.session_key = session_key or f"ask-{uuid.uuid4().hex[:12]}"
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self._token = token if token is not None else read_gateway_token()
        self._sent_message_count = 0
        # Injectable for tests so the adapter can be exercised without a
        # running Gateway.
        self._http_post = http_post
        self.chat = _ChatNamespace(self)

    # -- transport ---------------------------------------------------------- #

    def _post(self, payload: dict) -> dict:
        if self._http_post is not None:
            return self._http_post(payload)

        import httpx

        if not self._token:
            raise GatewayError(
                "OpenClaw Gateway token unavailable; cannot reach the agent. "
                "Check OPENCLAW_GATEWAY_TOKEN_FILE."
            )
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-openclaw-agent-id": self.agent_id,
            "x-openclaw-session-key": self.session_key,
        }
        response = httpx.post(
            f"{self.base_url}/v1/responses",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            # The token is rotated on service restart; re-read once.
            self._token = read_gateway_token()
            if not self._token:
                raise GatewayError("Gateway rejected the token and no new token is available.")
            headers["Authorization"] = f"Bearer {self._token}"
            response = httpx.post(
                f"{self.base_url}/v1/responses",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        if response.status_code >= 400:
            raise GatewayError(
                f"Gateway returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    # -- protocol translation ----------------------------------------------- #

    def create(self, *, messages: list[dict], tools: list[dict] | None = None, **kwargs) -> _Response:
        new_messages = messages[self._sent_message_count :]
        self._sent_message_count = len(messages)

        rendered = _render_turn(new_messages)
        if not rendered.strip():
            # Nothing new to say (e.g. only an assistant echo was appended).
            # Nudge rather than sending an empty input, which the Gateway rejects.
            rendered = "Continue."

        payload: dict[str, Any] = {
            "model": f"openclaw/{self.agent_id}",
            "input": rendered,
            "max_output_tokens": kwargs.get("max_tokens") or self.max_output_tokens,
        }
        # tool_choice="none" is how the loop asks for a wrap-up; sending tools
        # anyway would invite another tool call.
        if tools and kwargs.get("tool_choice") != "none":
            payload["tools"] = _chat_tools_to_responses(tools)
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        body = self._post(payload)
        return _Response(_parse_response(body))


class _ChatNamespace:
    """Mirrors ``client.chat.completions`` so the loop can stay transport-blind."""

    def __init__(self, adapter: GatewayChatAdapter):
        self.completions = adapter


def _parse_response(body: dict) -> _Choice:
    """Map an OpenResponses body onto a chat-completions choice."""

    texts: list[str] = []
    tool_calls: list[dict] = []

    for item in body.get("output", []) or []:
        if item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "call_0",
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
            continue
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str):
                texts.append(text)

    content_text = "".join(texts).strip() or None

    if tool_calls:
        finish_reason = "tool_calls"
    elif body.get("status") == "incomplete":
        finish_reason = "length"
    else:
        finish_reason = "stop"

    return _Choice(_Message(content_text, tool_calls or None), finish_reason)


def gateway_client_factory(
    session_key: str | None = None, **adapter_kwargs
):
    """Build a ``client_factory`` for ``run_agent`` bound to the Gateway.

    ``run_agent`` calls ``client_factory(base_url)``; the Gateway URL comes from
    the environment instead, so the argument is ignored.
    """

    def _factory(_base_url: str) -> GatewayChatAdapter:
        return GatewayChatAdapter(session_key=session_key, **adapter_kwargs)

    return _factory


__all__ = [
    "GatewayChatAdapter",
    "GatewayError",
    "gateway_client_factory",
    "read_gateway_token",
]
