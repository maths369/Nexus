from __future__ import annotations

import pytest

from nexus.agent.attempt import AttemptBuilder
from nexus.agent.tool_profiles import ToolProfile
from nexus.agent.types import ToolDefinition, ToolRiskLevel


class _StubMemoryManager:
    async def search(self, query: str, top_k: int = 5):  # noqa: ARG002
        return [
            {
                "content": "用户偏好简洁直接的沟通",
                "metadata": {"kind": "preference"},
            }
        ]


@pytest.mark.asyncio
async def test_attempt_builder_prefers_memory_manager_for_injection():
    builder = AttemptBuilder(
        available_tools=[
            ToolDefinition(
                name="dummy",
                description="dummy",
                parameters={"type": "object", "properties": {}},
                handler=lambda: None,
            )
        ],
        memory_manager=_StubMemoryManager(),
    )

    result = await builder._inject_memory("用户沟通偏好")  # noqa: SLF001

    assert "[preference] 用户偏好简洁直接的沟通" in result


def test_attempt_builder_coding_profile_keeps_dispatch_subagent():
    builder = AttemptBuilder(
        available_tools=[
            ToolDefinition(
                name="dispatch_subagent",
                description="delegate",
                parameters={"type": "object", "properties": {}},
                handler=lambda: None,
                risk_level=ToolRiskLevel.MEDIUM,
            ),
            ToolDefinition(
                name="file_write",
                description="write",
                parameters={"type": "object", "properties": {}},
                handler=lambda: None,
                risk_level=ToolRiskLevel.MEDIUM,
            ),
        ],
    )

    tools = builder._select_tools(  # noqa: SLF001
        "请调用子代理检查这个文件",
        tool_profile=ToolProfile.coding(),
    )

    assert {tool.name for tool in tools} == {"dispatch_subagent", "file_write"}
