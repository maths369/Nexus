"""SubagentRunner 测试 — 子任务委派"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from nexus.agent.session_manager import SessionManager
from nexus.agent.subagent_registry import SubagentRegistry
from nexus.agent.subagent import SubagentRunner, EXCLUDED_TOOLS
from nexus.agent.types import AttemptConfig, ToolDefinition, ToolRiskLevel
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_store import SessionStore
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


class FakeGatewayWithPrimary(FakeGateway):
    def __init__(self, responses: list[dict[str, Any]], *, model: str, name: str = "primary"):
        super().__init__(responses)
        self.primary_provider = type(
            "PrimaryProvider",
            (),
            {"model": model, "name": name},
        )()


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


def _make_tool(name: str) -> ToolDefinition:
    async def handler(**kwargs) -> str:
        return f"{name}: ok"
    return ToolDefinition(
        name=name,
        description=f"Test tool: {name}",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        risk_level=ToolRiskLevel.LOW,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dispatch_returns_summary():
    """子Agent 返回 summary"""
    gateway = FakeGateway([_text_response("分析完成：代码质量良好。")])
    policy = ToolsPolicy()
    runner = SubagentRunner(provider=gateway, tools_policy=policy)

    result = asyncio.run(runner.dispatch("分析代码质量"))
    assert "分析完成" in result
    assert runner.stats["dispatch_count"] == 1


def test_dispatch_with_tool_calls():
    """子Agent 可以调用工具"""
    gateway = FakeGateway([
        _tool_call_response("read_file", {"path": "main.py"}),
        _text_response("文件读取完成，共 100 行。"),
    ])
    policy = ToolsPolicy()
    tools = [_make_tool("read_file")]
    runner = SubagentRunner(provider=gateway, tools_policy=policy)

    result = asyncio.run(runner.dispatch("读取 main.py", tools=tools))
    assert "读取完成" in result


def test_dispatch_filters_excluded_tools():
    """子Agent 自动过滤递归和不必要的工具"""
    gateway = FakeGateway([_text_response("完成")])
    policy = ToolsPolicy()

    tools = [
        _make_tool("read_file"),
        _make_tool("dispatch_subagent"),  # 应被过滤
        _make_tool("compact"),            # 应被过滤
        _make_tool("todo_write"),         # 应被过滤
        _make_tool("search_vault"),
    ]
    runner = SubagentRunner(provider=gateway, tools_policy=policy)
    asyncio.run(runner.dispatch("测试", tools=tools))

    # 检查传给 LLM 的工具
    tools_sent = gateway.calls[0].get("tools", [])
    tool_names = {t["function"]["name"] for t in tools_sent}
    assert "read_file" in tool_names
    assert "search_vault" in tool_names
    assert "dispatch_subagent" not in tool_names
    assert "compact" not in tool_names
    assert "todo_write" not in tool_names


def test_dispatch_uses_fresh_context():
    """子Agent 使用全新 context，不继承父 Agent 消息"""
    gateway = FakeGateway([_text_response("子任务完成")])
    policy = ToolsPolicy()
    runner = SubagentRunner(provider=gateway, tools_policy=policy)

    asyncio.run(runner.dispatch("执行子任务"))

    # 检查传给 LLM 的 messages：前两条应为 system + user（隔离 context）
    # 注意: messages 列表会在 call 后被 execute_tool_loop 追加 assistant 响应
    messages = gateway.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "执行子任务" in messages[1]["content"]
    # 不应包含任何先前对话的 user/assistant 消息
    pre_call_msgs = [m for m in messages if m["role"] in ("system", "user")
                     and "tool_calls" not in m]
    assert len(pre_call_msgs) == 2


def test_dispatch_handles_error_gracefully():
    """子Agent 执行失败时返回错误消息而不是抛异常"""
    class ErrorGateway:
        async def chat_completion(self, **kwargs):
            raise RuntimeError("模型不可用")

    policy = ToolsPolicy()
    runner = SubagentRunner(provider=ErrorGateway(), tools_policy=policy)

    result = asyncio.run(runner.dispatch("会失败的任务"))
    assert "失败" in result
    assert "模型不可用" in result
    assert runner.stats["dispatch_count"] == 1


def test_dispatch_empty_response():
    """子Agent 空响应时返回默认提示"""
    gateway = FakeGateway([_text_response("")])
    policy = ToolsPolicy()
    runner = SubagentRunner(provider=gateway, tools_policy=policy)

    result = asyncio.run(runner.dispatch("测试"))
    assert "未返回摘要" in result


def test_dispatch_custom_model():
    """可以指定子Agent 使用不同模型"""
    gateway = FakeGateway([_text_response("完成")])
    policy = ToolsPolicy()
    runner = SubagentRunner(
        provider=gateway,
        tools_policy=policy,
        model="default-model",
    )

    asyncio.run(runner.dispatch("测试"))
    assert gateway.calls[0]["model"] == "default-model"


def test_dispatch_defaults_to_provider_primary_model():
    gateway = FakeGatewayWithPrimary([_text_response("完成")], model="qwen3.5-plus")
    policy = ToolsPolicy()
    runner = SubagentRunner(provider=gateway, tools_policy=policy)

    asyncio.run(runner.dispatch("测试"))

    assert gateway.calls[0]["model"] == "qwen3.5-plus"


def test_dispatch_description_in_logs():
    """description 用于日志（不影响执行）"""
    gateway = FakeGateway([_text_response("完成")])
    policy = ToolsPolicy()
    runner = SubagentRunner(provider=gateway, tools_policy=policy)

    # 不应抛异常
    result = asyncio.run(runner.dispatch(
        "分析所有测试文件",
        description="分析测试覆盖率",
    ))
    assert result == "完成"


def test_excluded_tools_constant():
    """确认 EXCLUDED_TOOLS 包含正确的工具名"""
    assert "dispatch_subagent" in EXCLUDED_TOOLS
    assert "compact" in EXCLUDED_TOOLS
    assert "todo_write" in EXCLUDED_TOOLS


def test_dispatch_session_mode_persists_context(tmp_path):
    gateway = FakeGateway([
        _text_response("第一次结论"),
        _text_response("第二次结论"),
    ])
    policy = ToolsPolicy()
    store = SessionStore(tmp_path / "sessions.db")
    context = ContextWindowManager(store)
    manager = SessionManager(store, None)
    registry = SubagentRegistry(tmp_path / "subagents")
    parent = store.create_session("user-1", "feishu", summary="父会话")
    runner = SubagentRunner(
        provider=gateway,
        tools_policy=policy,
        session_store=store,
        session_manager=manager,
        context_window=context,
        registry=registry,
    )

    first = asyncio.run(
        runner.dispatch(
            "先分析模块 A",
            spawn_mode="session",
            parent_session_id=parent.session_id,
        )
    )
    assert "子Agent(session)" in first
    session_id = first.split("`")[1]

    second = asyncio.run(
        runner.dispatch(
            "继续补充模块 B",
            spawn_mode="session",
            session_id=session_id,
            parent_session_id=parent.session_id,
        )
    )

    assert "第二次结论" in second
    child = store.get_session(session_id)
    assert child is not None
    events = store.get_events(session_id)
    assert len([event for event in events if event.role == "assistant"]) == 2
    parent_events = store.get_events(parent.session_id)
    assert any("subagent:" in event.content for event in parent_events if event.role == "system")
