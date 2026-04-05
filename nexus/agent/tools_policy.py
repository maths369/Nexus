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

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any

from .types import ToolCall, ToolDefinition, ToolRiskLevel

logger = logging.getLogger(__name__)


@dataclass
class PolicyCheckResult:
    """治理检查结果"""
    allowed: bool
    reason: str = ""
    requires_approval: bool = False


_RISK_ORDER = {
    ToolRiskLevel.LOW: 0,
    ToolRiskLevel.MEDIUM: 1,
    ToolRiskLevel.HIGH: 2,
    ToolRiskLevel.CRITICAL: 3,
}


def _risk_order(level: ToolRiskLevel | None) -> int:
    if level is None:
        return 99
    return _RISK_ORDER.get(level, 99)


def _matches_any(name: str, patterns: list[str] | tuple[str, ...] | set[str]) -> bool:
    if not patterns:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


@dataclass
class PolicyLayer:
    """
    单层策略：支持 allow/deny/also_allow + 风险和数量约束。

    语义:
    - allow=None: 不限制
    - allow=[]: 全拒绝
    - deny 优先于 also_allow
    - also_allow 可用于从上游收窄后重新放回工具
    """

    name: str
    allow: list[str] | None = None
    deny: list[str] = field(default_factory=list)
    also_allow: list[str] = field(default_factory=list)
    max_risk_level: ToolRiskLevel | None = None
    max_tools_count: int | None = None

    def filter_tools(
        self,
        tools: list[ToolDefinition],
        *,
        universe: list[ToolDefinition] | None = None,
    ) -> list[ToolDefinition]:
        result: list[ToolDefinition] = []
        seen: set[str] = set()

        for tool in tools:
            if _matches_any(tool.name, self.deny):
                continue
            if _matches_any(tool.name, self.also_allow):
                result.append(tool)
                seen.add(tool.name)
                continue
            if self.allow is not None and not _matches_any(tool.name, self.allow):
                continue
            if self.max_risk_level is not None and _risk_order(tool.risk_level) > _risk_order(self.max_risk_level):
                continue
            result.append(tool)
            seen.add(tool.name)

        if universe and self.also_allow:
            for tool in universe:
                if tool.name in seen:
                    continue
                if _matches_any(tool.name, self.deny):
                    continue
                if not _matches_any(tool.name, self.also_allow):
                    continue
                if self.max_risk_level is not None and _risk_order(tool.risk_level) > _risk_order(self.max_risk_level):
                    continue
                result.append(tool)
                seen.add(tool.name)

        if self.max_tools_count is not None and len(result) > self.max_tools_count:
            return result[: self.max_tools_count]
        return result


class ToolPolicyPipeline:
    """多层 AND 过滤管线。"""

    def __init__(
        self,
        layers: list[PolicyLayer] | None = None,
        *,
        frequency_limit: int = 50,
    ):
        self._layers: list[PolicyLayer] = list(layers or [])
        self._call_counts: dict[str, int] = {}
        self._frequency_limit = frequency_limit

    def add_layer(self, layer: PolicyLayer) -> None:
        self._layers.append(layer)

    def with_layers(self, *layers: PolicyLayer) -> ToolPolicyPipeline:
        return ToolPolicyPipeline(
            [*self._layers, *layers],
            frequency_limit=self._frequency_limit,
        )

    def filter_tools(self, tools: list[ToolDefinition]) -> list[ToolDefinition]:
        current = list(tools)
        universe = list(tools)
        for layer in self._layers:
            current = layer.filter_tools(current, universe=universe)
        return current

    async def check_runtime(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
    ) -> PolicyCheckResult:
        count = self._call_counts.get(call.tool_name, 0)
        if count >= self._frequency_limit:
            return PolicyCheckResult(
                allowed=False,
                reason=f"Tool '{call.tool_name}' exceeded call limit ({self._frequency_limit})",
            )
        self._call_counts[call.tool_name] = count + 1

        if tool_def.risk_level in (ToolRiskLevel.HIGH, ToolRiskLevel.CRITICAL):
            return PolicyCheckResult(
                allowed=True,
                requires_approval=True,
                reason=f"Tool '{call.tool_name}' requires approval ({tool_def.risk_level.value})",
            )
        return PolicyCheckResult(allowed=True)

    async def check(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
    ) -> PolicyCheckResult:
        return await self.check_runtime(call, tool_def)

    def reset_counters(self) -> None:
        self._call_counts.clear()


class ToolsPolicy(ToolPolicyPipeline):
    """
    向后兼容的工具治理策略。

    保留历史接口:
    - whitelist / blacklist
    - auto_approve_levels
    - check()
    - reset_counts()
    """

    def __init__(
        self,
        blacklist: set[str] | None = None,
        whitelist: set[str] | None = None,
        auto_approve_levels: set[ToolRiskLevel] | None = None,
    ):
        layers: list[PolicyLayer] = []
        if whitelist is not None or blacklist:
            layers.append(
                PolicyLayer(
                    name="legacy-global",
                    allow=sorted(whitelist) if whitelist is not None else None,
                    deny=sorted(blacklist or []),
                )
            )
        super().__init__(layers)
        self._blacklist = blacklist or set()
        self._whitelist = whitelist  # None 表示不启用白名单
        self._auto_approve = auto_approve_levels or {
            ToolRiskLevel.LOW,
            ToolRiskLevel.MEDIUM,
        }

    async def check_runtime(
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

    async def check(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
    ) -> PolicyCheckResult:
        return await self.check_runtime(call, tool_def)

    def with_layers(self, *layers: PolicyLayer) -> ToolsPolicy:
        clone = ToolsPolicy(
            blacklist=set(self._blacklist),
            whitelist=set(self._whitelist) if self._whitelist is not None else None,
            auto_approve_levels=set(self._auto_approve),
        )
        clone._layers = [*self._layers, *layers]
        clone._frequency_limit = self._frequency_limit
        return clone

    def reset_counts(self) -> None:
        """重置调用计数（新 Run 开始时调用）"""
        self.reset_counters()

    def add_to_blacklist(self, tool_name: str) -> None:
        """动态添加黑名单"""
        self._blacklist.add(tool_name)
        if self._layers:
            deny = list(self._layers[0].deny)
            if tool_name not in deny:
                deny.append(tool_name)
                self._layers[0].deny = deny
        logger.info(f"Tool '{tool_name}' added to blacklist")

    def remove_from_blacklist(self, tool_name: str) -> None:
        """从黑名单移除"""
        self._blacklist.discard(tool_name)
        if self._layers:
            self._layers[0].deny = [item for item in self._layers[0].deny if item != tool_name]
        logger.info(f"Tool '{tool_name}' removed from blacklist")
