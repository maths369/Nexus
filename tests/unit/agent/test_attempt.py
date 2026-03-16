from __future__ import annotations

import pytest

from nexus.agent.attempt import AttemptBuilder
from nexus.agent.types import ToolDefinition


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
