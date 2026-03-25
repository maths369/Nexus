"""
Approval Engine — Hub 层审批引擎

对标 OpenClaw 的 Approval Engine:
- 工具调用执行前检查是否需要审批
- 需要审批时暂停 Run，等待用户响应
- 支持超时自动拒绝
- 通过 Channel 向用户发送审批请求
- 支持批量审批（"允许本次 Run 内所有同类调用"）

与 ToolsPolicy 的关系:
- ToolsPolicy 做静态治理检查（黑/白名单、频率限制）
- ApprovalEngine 做动态人机交互审批（HIGH/CRITICAL 工具的确认流程）

用法:
    engine = ApprovalEngine(timeout=120)
    result = await engine.request_approval(tool_call, tool_def, run_id)
    if result.approved:
        # 执行工具
    else:
        # 拒绝并返回错误
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .types import ToolCall, ToolDefinition, ToolRiskLevel

logger = logging.getLogger(__name__)


class ApprovalStatus(str, enum.Enum):
    """审批状态"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    AUTO_APPROVED = "auto_approved"


@dataclass
class ApprovalRequest:
    """审批请求"""
    approval_id: str
    run_id: str
    tool_name: str
    tool_description: str
    arguments: dict[str, Any]
    risk_level: str
    reason: str
    requested_at: float  # time.time()
    timeout_seconds: float = 120.0
    # 来源信息
    session_id: str | None = None
    channel: str | None = None  # "feishu" | "web" | "desktop" | ...


@dataclass
class ApprovalResult:
    """审批结果"""
    approval_id: str
    status: ApprovalStatus
    approved: bool
    comment: str | None = None
    resolved_at: float | None = None
    # 批量授权: 允许同一 Run 内后续的同类调用
    allow_remaining_in_run: bool = False


# 审批回调类型: 通知外部系统（Channel、UI）有新的审批请求
ApprovalNotifier = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalEngine:
    """
    Hub 层审批引擎。

    当 ToolsPolicy 返回 requires_approval=True 时:
    1. 创建 ApprovalRequest
    2. 通过 notifier 回调通知用户（Channel 消息、Desktop UI 等）
    3. 阻塞等待用户响应或超时
    4. 返回 ApprovalResult

    特殊逻辑:
    - 用户选择"允许后续同类调用"后，同一 Run 内的该工具自动通过
    - 超时自动拒绝
    """

    def __init__(
        self,
        *,
        default_timeout: float = 120.0,
        notifier: ApprovalNotifier | None = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._notifier = notifier
        # 待审批请求: approval_id -> (request, future)
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalResult]]] = {}
        # 批量授权缓存: (run_id, tool_name)
        self._auto_approved: set[tuple[str, str]] = set()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def list_pending(self) -> list[ApprovalRequest]:
        """列出所有待审批请求"""
        return [req for req, _ in self._pending.values()]

    async def request_approval(
        self,
        call: ToolCall,
        tool_def: ToolDefinition,
        run_id: str,
        *,
        session_id: str | None = None,
        channel: str | None = None,
        timeout: float | None = None,
    ) -> ApprovalResult:
        """
        请求审批。阻塞直到获得响应或超时。

        Returns:
            ApprovalResult — approved=True 表示可以执行
        """
        # 检查批量授权缓存
        cache_key = (run_id, call.tool_name)
        if cache_key in self._auto_approved:
            logger.info(
                "工具 '%s' 在 Run '%s' 中已获得批量授权，自动通过",
                call.tool_name, run_id,
            )
            return ApprovalResult(
                approval_id=f"auto-{uuid.uuid4().hex[:8]}",
                status=ApprovalStatus.AUTO_APPROVED,
                approved=True,
                comment="批量授权自动通过",
                resolved_at=time.time(),
            )

        # 创建审批请求
        approval_id = uuid.uuid4().hex[:12]
        effective_timeout = timeout or self._default_timeout

        request = ApprovalRequest(
            approval_id=approval_id,
            run_id=run_id,
            tool_name=call.tool_name,
            tool_description=tool_def.description,
            arguments=call.arguments,
            risk_level=tool_def.risk_level.value,
            reason=self._build_reason(call, tool_def),
            requested_at=time.time(),
            timeout_seconds=effective_timeout,
            session_id=session_id,
            channel=channel,
        )

        # 创建 Future
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalResult] = loop.create_future()
        self._pending[approval_id] = (request, future)

        # 通知外部
        if self._notifier:
            try:
                await self._notifier(request)
            except Exception as e:
                logger.error("审批通知发送失败: %s", e)

        logger.info(
            "审批请求已创建: %s (工具=%s, 风险=%s, 超时=%ds)",
            approval_id, call.tool_name, tool_def.risk_level.value, effective_timeout,
        )

        # 等待响应或超时
        try:
            result = await asyncio.wait_for(future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            result = ApprovalResult(
                approval_id=approval_id,
                status=ApprovalStatus.TIMEOUT,
                approved=False,
                comment=f"审批超时 ({effective_timeout}s)",
                resolved_at=time.time(),
            )
            logger.warning("审批请求超时: %s", approval_id)
        finally:
            self._pending.pop(approval_id, None)

        # 处理批量授权
        if result.approved and result.allow_remaining_in_run:
            self._auto_approved.add(cache_key)
            logger.info(
                "工具 '%s' 在 Run '%s' 中获得批量授权",
                call.tool_name, run_id,
            )

        return result

    async def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        comment: str | None = None,
        allow_remaining: bool = False,
    ) -> ApprovalRequest | None:
        """
        解决一个审批请求。

        由外部系统调用（Channel 适配器、Desktop UI、API 端点等）。

        Returns:
            解决的 ApprovalRequest，如果 approval_id 不存在则返回 None
        """
        entry = self._pending.get(approval_id)
        if entry is None:
            logger.warning("审批请求不存在或已解决: %s", approval_id)
            return None

        request, future = entry
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

        result = ApprovalResult(
            approval_id=approval_id,
            status=status,
            approved=approved,
            comment=comment,
            resolved_at=time.time(),
            allow_remaining_in_run=allow_remaining,
        )

        if not future.done():
            future.set_result(result)

        logger.info(
            "审批请求已解决: %s → %s (allow_remaining=%s)",
            approval_id, status.value, allow_remaining,
        )
        return request

    def clear_run_cache(self, run_id: str) -> None:
        """清除指定 Run 的批量授权缓存"""
        keys_to_remove = {k for k in self._auto_approved if k[0] == run_id}
        self._auto_approved -= keys_to_remove
        if keys_to_remove:
            logger.debug("清除 Run '%s' 的 %d 条批量授权", run_id, len(keys_to_remove))

    def clear_all(self) -> None:
        """清除所有待审批请求和缓存"""
        # 取消所有待审批的 Future
        for approval_id, (request, future) in self._pending.items():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._auto_approved.clear()

    def to_approval_card(self, request: ApprovalRequest) -> dict[str, Any]:
        """
        将审批请求转换为通用卡片格式（供 Channel 适配器渲染）。

        返回结构化数据，Channel 适配器可以将其转换为:
        - 飞书: 交互式卡片（带 approve/reject 按钮）
        - Desktop: SwiftUI 审批视图
        - Web: HTML 卡片
        """
        # 参数摘要（截断长参数）
        args_summary = {}
        for k, v in request.arguments.items():
            sv = str(v)
            if len(sv) > 200:
                sv = sv[:200] + "..."
            args_summary[k] = sv

        risk_emoji = {
            "low": "",
            "medium": "",
            "high": "[HIGH]",
            "critical": "[CRITICAL]",
        }.get(request.risk_level, "")

        return {
            "type": "approval_card",
            "approval_id": request.approval_id,
            "title": f"{risk_emoji} 工具审批: {request.tool_name}",
            "description": request.tool_description,
            "risk_level": request.risk_level,
            "reason": request.reason,
            "arguments": args_summary,
            "timeout_seconds": request.timeout_seconds,
            "actions": [
                {"action": "approve", "label": "批准"},
                {"action": "approve_all", "label": "批准(本次全部)"},
                {"action": "reject", "label": "拒绝"},
            ],
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reason(call: ToolCall, tool_def: ToolDefinition) -> str:
        """构建审批理由"""
        parts = [f"工具 '{call.tool_name}' 风险等级为 {tool_def.risk_level.value}"]

        # 根据工具类型提供更具体的理由
        if call.tool_name == "system_run":
            cmd = call.arguments.get("command", "")
            parts.append(f"将执行 shell 命令: {cmd[:100]}")
        elif call.tool_name == "delete_page":
            title = call.arguments.get("title", "")
            parts.append(f"将删除页面: {title}")
        elif "install" in call.tool_name:
            parts.append("将安装新组件")

        return "。".join(parts) + "。"
