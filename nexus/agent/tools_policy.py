"""
Tools Policy — 工具治理策略管线

职责:
1. 工具白名单/黑名单管理
2. 风险评估
3. 审批流程（高风险工具需要用户确认）
4. 调用频率限制

迁移来源: macos-ai-assistant/orchestrator/services/tool_governance.py (745 行)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .types import ToolCall, ToolDefinition, ToolRiskLevel

logger = logging.getLogger(__name__)


@dataclass
class PolicyCheckResult:
    """治理检查结果"""
    allowed: bool
    reason: str = ""
    requires_approval: bool = False


class ToolsPolicy:
    """
    工具治理策略管线。

    执行顺序:
    1. 黑名单检查
    2. 白名单检查（如果启用）
    3. 风险等级评估
    4. 审批需求检查
    5. 频率限制检查
    """

    def __init__(
        self,
        blacklist: set[str] | None = None,
        whitelist: set[str] | None = None,
        auto_approve_levels: set[ToolRiskLevel] | None = None,
    ):
        self._blacklist = blacklist or set()
        self._whitelist = whitelist  # None 表示不启用白名单
        self._auto_approve = auto_approve_levels or {
            ToolRiskLevel.LOW,
            ToolRiskLevel.MEDIUM,
        }
        # 调用计数（简单的内存计数，未来可迁移到 SQLite）
        self._call_counts: dict[str, int] = {}

    async def check(
        self, call: ToolCall, tool_def: ToolDefinition
    ) -> PolicyCheckResult:
        """
        对工具调用执行治理检查。

        返回 PolicyCheckResult 指示是否允许调用。
        """
        # Step 1: 黑名单
        if call.tool_name in self._blacklist:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Tool '{call.tool_name}' is blacklisted",
            )

        # Step 2: 白名单
        if self._whitelist is not None and call.tool_name not in self._whitelist:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Tool '{call.tool_name}' is not in whitelist",
            )

        # Remote mesh tools may require approval on the target node.
        # In that case the Hub should not preemptively block them here;
        # the edge node owns the real approval interaction.
        if tool_def.requires_approval and (
            call.tool_name.startswith("mesh__") or "mesh" in tool_def.tags
        ):
            count = self._call_counts.get(call.tool_name, 0)
            max_calls = 50
            if count >= max_calls:
                return PolicyCheckResult(
                    allowed=False,
                    reason=f"Tool '{call.tool_name}' exceeded call limit ({max_calls})",
                )
            self._call_counts[call.tool_name] = count + 1
            return PolicyCheckResult(allowed=True)

        # Step 3: 风险等级
        if tool_def.risk_level not in self._auto_approve:
            if tool_def.risk_level == ToolRiskLevel.CRITICAL:
                return PolicyCheckResult(
                    allowed=False,
                    reason=f"Tool '{call.tool_name}' requires human approval (CRITICAL)",
                    requires_approval=True,
                )
            if tool_def.risk_level == ToolRiskLevel.HIGH:
                # HIGH 风险工具：当前阻断并标记需要审批
                # 未来可以通过 Channel 向用户请求确认
                return PolicyCheckResult(
                    allowed=False,
                    reason=f"Tool '{call.tool_name}' requires approval (HIGH risk)",
                    requires_approval=True,
                )

        # Step 4: 频率限制（简单实现）
        count = self._call_counts.get(call.tool_name, 0)
        max_calls = 50  # 单次 run 中每个工具最多调用次数
        if count >= max_calls:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Tool '{call.tool_name}' exceeded call limit ({max_calls})",
            )

        # 更新计数
        self._call_counts[call.tool_name] = count + 1

        return PolicyCheckResult(allowed=True)

    def reset_counts(self) -> None:
        """重置调用计数（新 Run 开始时调用）"""
        self._call_counts.clear()

    def add_to_blacklist(self, tool_name: str) -> None:
        """动态添加黑名单"""
        self._blacklist.add(tool_name)
        logger.info(f"Tool '{tool_name}' added to blacklist")

    def remove_from_blacklist(self, tool_name: str) -> None:
        """从黑名单移除"""
        self._blacklist.discard(tool_name)
        logger.info(f"Tool '{tool_name}' removed from blacklist")
