"""Agent Core tool loop tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

from nexus.agent.core import execute_tool_loop, CONTEXT_TOKEN_LIMIT
from nexus.agent.types import (
    AttemptConfig,
    ContextOverflowError,
    ProviderError,
    ToolDefinition,
    ToolRiskLevel,
)
from nexus.agent.tools_policy import ToolsPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeGateway:
    """Fake ProviderGateway that returns scripted responses."""

    def __init__(self, responses: list[dict[str, Any]]):
        self._responses = list(responses)
        self._call_index = 0
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._call_index >= len(self._responses):
            return {
                "message": {
                    "role": "assistant",
                    "content": "done",
                    "tool_calls": [],
                }
            }
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp


class ErrorGateway:
    """Fake gateway that raises an error."""

    def __init__(self, error_message: str):
        self._error = error_message

    async def chat_completion(self, **kwargs):
        raise RuntimeError(self._error)


def make_tool(name: str = "echo", risk: ToolRiskLevel = ToolRiskLevel.LOW):
    """Create a simple async tool for testing."""

    async def handler(text: str = "") -> str:
        return f"echo: {text}"

    return ToolDefinition(
        name=name,
        description=f"Test tool: {name}",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=handler,
        risk_level=risk,
    )


def _text_response(content: str) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": [],
        }
    }


def _tool_call_response(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                }
            ],
        }
    }


def _config(tools=None, messages=None) -> AttemptConfig:
    return AttemptConfig(
        model="test-model",
        system_prompt="You are a test agent.",
        tools=tools or [],
        messages=messages or [{"role": "user", "content": "hello"}],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tool_loop_returns_text_when_no_tools_called():
    gateway = FakeGateway([_text_response("simple answer")])
    policy = ToolsPolicy()

    result_text, events = asyncio.run(
        execute_tool_loop(_config(), gateway, policy, "run-1")
    )

    assert result_text == "simple answer"
    assert len(events) == 1
    assert events[0].event_type == "llm_response"


def test_tool_loop_executes_tool_and_continues():
    """LLM calls a tool, gets result, then gives final answer."""
    gateway = FakeGateway([
        _tool_call_response("echo", {"text": "hello world"}),
        _text_response("Tool said: echo: hello world"),
    ])
    tool = make_tool("echo")
    policy = ToolsPolicy()

    result_text, events = asyncio.run(
        execute_tool_loop(_config(tools=[tool]), gateway, policy, "run-2")
    )

    assert result_text == "Tool said: echo: hello world"
    # Should have: llm_response(tool_call) + tool_call + tool_result + llm_response(final)
    assert len(events) == 4
    assert events[0].event_type == "llm_response"
    assert events[1].event_type == "tool_call"
    assert events[1].data["tool"] == "echo"
    assert events[1].data["call_id"]
    assert events[2].event_type == "tool_result"
    assert events[2].data["tool"] == "echo"
    assert events[2].data["call_id"] == events[1].data["call_id"]
    assert events[2].data["success"] is True
    assert events[3].event_type == "llm_response"


def test_tool_loop_blocks_unknown_tool():
    """LLM calls a tool that doesn't exist in the tool map."""
    gateway = FakeGateway([
        _tool_call_response("nonexistent_tool", {}),
        _text_response("ok"),
    ])
    policy = ToolsPolicy()

    result_text, events = asyncio.run(
        execute_tool_loop(_config(), gateway, policy, "run-3")
    )

    tool_results = [e for e in events if e.event_type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].data["success"] is False
    assert tool_results[0].data["error"] == "Unknown tool: nonexistent_tool"


def test_tool_loop_blocks_high_risk_tool():
    """High risk tool should be blocked by default policy."""
    gateway = FakeGateway([
        _tool_call_response("dangerous", {"text": "delete all"}),
        _text_response("blocked"),
    ])
    tool = make_tool("dangerous", risk=ToolRiskLevel.HIGH)
    policy = ToolsPolicy()

    result_text, events = asyncio.run(
        execute_tool_loop(_config(tools=[tool]), gateway, policy, "run-4")
    )

    blocked = [e for e in events if e.event_type == "tool_blocked"]
    assert len(blocked) == 1
    assert "approval" in blocked[0].data["reason"].lower()


def test_tool_loop_allows_remote_mesh_tool_that_requires_target_approval():
    async def remote_handler(**kwargs):
        return "remote approval queued"

    tool = ToolDefinition(
        name="mesh__bWFjYm9vay1wcm8__run_applescript",
        description="Remote AppleScript on MacBook Pro",
        parameters={"type": "object", "properties": {"script": {"type": "string"}}},
        handler=remote_handler,
        risk_level=ToolRiskLevel.CRITICAL,
        requires_approval=True,
        tags=["mesh", "node:macbook-pro", "tool:run_applescript"],
    )
    gateway = FakeGateway([
        _tool_call_response(tool.name, {"script": "tell application \"Google Chrome\" to activate"}),
        _text_response("approval requested on target mac"),
    ])
    policy = ToolsPolicy()

    result_text, events = asyncio.run(
        execute_tool_loop(_config(tools=[tool]), gateway, policy, "run-4b")
    )

    assert result_text == "approval requested on target mac"
    blocked = [e for e in events if e.event_type == "tool_blocked"]
    assert blocked == []
    tool_results = [e for e in events if e.event_type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].data["success"] is True


def test_tool_loop_raises_context_overflow():
    """Should raise ContextOverflowError when messages are too large."""
    # Create messages that exceed the token limit
    huge_content = "x" * (CONTEXT_TOKEN_LIMIT * 3)
    big_messages = [{"role": "user", "content": huge_content}]
    config = _config(messages=big_messages)

    gateway = FakeGateway([_text_response("should not reach")])
    policy = ToolsPolicy()

    try:
        asyncio.run(execute_tool_loop(config, gateway, policy, "run-5"))
        assert False, "Should have raised ContextOverflowError"
    except ContextOverflowError:
        pass  # expected


def test_tool_loop_raises_provider_error_on_gateway_failure():
    """Gateway errors should be wrapped as ProviderError."""
    gateway = ErrorGateway("connection refused")
    policy = ToolsPolicy()

    try:
        asyncio.run(execute_tool_loop(_config(), gateway, policy, "run-6"))
        assert False, "Should have raised ProviderError"
    except ProviderError as e:
        assert "connection refused" in str(e)


def test_tool_loop_raises_context_overflow_on_context_length_error():
    """Provider returning context_length_exceeded should become ContextOverflowError."""
    gateway = ErrorGateway("context_length_exceeded: max 128000 tokens")
    policy = ToolsPolicy()

    try:
        asyncio.run(execute_tool_loop(_config(), gateway, policy, "run-7"))
        assert False, "Should have raised ContextOverflowError"
    except ContextOverflowError:
        pass  # expected


def test_tool_loop_handles_tool_execution_error_gracefully():
    """If a tool handler throws, it should be caught and returned as error result."""

    async def failing_handler(**kwargs):
        raise ValueError("disk full")

    tool = ToolDefinition(
        name="failing",
        description="Always fails",
        parameters={"type": "object", "properties": {}},
        handler=failing_handler,
    )

    gateway = FakeGateway([
        _tool_call_response("failing", {}),
        _text_response("tool failed"),
    ])
    policy = ToolsPolicy()

    result_text, events = asyncio.run(
        execute_tool_loop(_config(tools=[tool]), gateway, policy, "run-8")
    )

    tool_results = [e for e in events if e.event_type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].data["success"] is False
    assert "disk full" in tool_results[0].data["error"]


def test_tool_loop_passes_tools_to_gateway():
    """Tool schemas should be passed to the gateway."""
    tool = make_tool("read_file")
    gateway = FakeGateway([_text_response("ok")])
    policy = ToolsPolicy()

    asyncio.run(
        execute_tool_loop(_config(tools=[tool]), gateway, policy, "run-9")
    )

    assert len(gateway.calls) == 1
    tools_sent = gateway.calls[0].get("tools", [])
    assert len(tools_sent) == 1
    assert tools_sent[0]["function"]["name"] == "read_file"


def test_tool_loop_serializes_tool_call_arguments_before_next_provider_round():
    gateway = FakeGateway([
        _tool_call_response("echo", {"text": "hello world"}),
        _text_response("done"),
    ])
    tool = make_tool("echo")
    policy = ToolsPolicy()

    result_text, _events = asyncio.run(
        execute_tool_loop(_config(tools=[tool]), gateway, policy, "run-10")
    )

    assert result_text == "done"
    assert len(gateway.calls) == 2
    second_messages = gateway.calls[1]["messages"]
    assistant_with_tool = next(
        msg for msg in second_messages
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    )
    assert assistant_with_tool["role"] == "assistant"
    assert isinstance(assistant_with_tool["tool_calls"][0]["function"]["arguments"], str)
    assert json.loads(assistant_with_tool["tool_calls"][0]["function"]["arguments"]) == {
        "text": "hello world"
    }
