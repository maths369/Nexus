"""
Subagent — 子任务委派

参考: learn-claude-code s04_subagent.py

核心机制:
1. 父 Agent 通过 dispatch_subagent 工具派发子任务
2. 子 Agent 使用全新 messages=[]，独立 context
3. 子 Agent 共享文件系统和工具集（排除递归/压缩/进度工具）
4. 只返回 summary 给父 Agent，子 context 丢弃

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: subagent   | ---------->| execute_tool_loop|
    |   prompt="..."   |            |   call tools     |
    |                  |  summary   |   append results |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

核心洞见: "上下文隔离让父 Agent 保持干净。"
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

from .types import AttemptConfig, ToolDefinition

if TYPE_CHECKING:
    from nexus.provider.gateway import ProviderGateway
    from .tools_policy import ToolsPolicy

logger = logging.getLogger(__name__)

# 子 Agent 不允许使用的工具（防止递归和不必要的开销）
EXCLUDED_TOOLS = frozenset({
    "dispatch_subagent",  # 禁止递归生成子Agent
    "compact",            # 子Agent 寿命短，不需要压缩
    "todo_write",         # 进度由父Agent追踪
})

# 子 Agent 默认最大迭代次数
SUBAGENT_MAX_ITERATIONS = 20

DEFAULT_SUBAGENT_SYSTEM = (
    "你是一个子任务执行Agent。\n"
    "完成给定任务后，用简洁的文字总结你的发现和结果。\n"
    "只关注被分配的具体子任务，不要扩展范围。"
)


class SubagentRunner:
    """
    子 Agent 执行器。

    接收父 Agent 的 prompt，在隔离 context 中执行，只返回 summary。
    子 Agent 使用父 Agent 的 provider 和 tools_policy，但拥有独立的消息列表。
    """

    def __init__(
        self,
        provider: ProviderGateway,
        tools_policy: ToolsPolicy,
        model: str = "qwen-max",
        system_prompt: str = "",
        max_iterations: int = SUBAGENT_MAX_ITERATIONS,
    ):
        self._provider = provider
        self._policy = tools_policy
        self._model = model
        self._system_prompt = system_prompt or DEFAULT_SUBAGENT_SYSTEM
        self._max_iterations = max_iterations
        self._dispatch_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"dispatch_count": self._dispatch_count}

    async def dispatch(
        self,
        prompt: str,
        description: str = "",
        model: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> str:
        """
        以全新 context 执行子任务，返回 summary。

        Args:
            prompt: 子任务指令
            description: 子任务简短描述（用于日志）
            model: 可选指定模型（默认使用构造时的模型）
            tools: 可用工具列表（自动过滤掉不允许的工具）

        Returns:
            子 Agent 的最终回复文本
        """
        from .core import execute_tool_loop, MAX_TOOL_ITERATIONS

        # 过滤工具：排除递归和不必要的工具
        child_tools = [
            t for t in (tools or [])
            if t.name not in EXCLUDED_TOOLS
        ]

        subagent_id = f"sub-{uuid.uuid4().hex[:8]}"
        desc_label = description or prompt[:60]
        logger.info(f"[{subagent_id}] Subagent dispatched: {desc_label}")

        # 构建全新的 AttemptConfig（隔离 context）
        config = AttemptConfig(
            model=model or self._model,
            system_prompt=self._system_prompt,
            tools=child_tools,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
        )

        try:
            # 使用父 Agent 的 provider 和 policy，但不传 compressor/todo
            result_text, events = await execute_tool_loop(
                config=config,
                provider=self._provider,
                tools_policy=self._policy,
                run_id=subagent_id,
                # 子 Agent 不需要压缩器和进度追踪
            )
            self._dispatch_count += 1

            summary = result_text or "(子Agent未返回摘要)"
            logger.info(
                f"[{subagent_id}] Subagent completed, "
                f"summary length={len(summary)}, events={len(events)}"
            )
            return summary

        except Exception as e:
            logger.error(f"[{subagent_id}] Subagent failed: {e}", exc_info=True)
            self._dispatch_count += 1
            return f"子Agent执行失败: {e}"
