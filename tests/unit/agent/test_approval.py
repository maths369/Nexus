"""Approval Engine 单元测试"""

import asyncio
import pytest

from nexus.agent.approval import (
    ApprovalEngine,
    ApprovalRequest,
    ApprovalResult,
    ApprovalStatus,
)
from nexus.agent.types import ToolCall, ToolDefinition, ToolRiskLevel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_tool_call(name: str = "system_run", **kwargs) -> ToolCall:
    return ToolCall(
        call_id="call-001",
        tool_name=name,
        arguments={"command": "rm -rf /tmp/test", **kwargs},
    )


def _make_tool_def(
    name: str = "system_run",
    risk: ToolRiskLevel = ToolRiskLevel.HIGH,
) -> ToolDefinition:
    async def _noop(**kw):
        return "ok"
    return ToolDefinition(
        name=name,
        description=f"执行 {name}",
        parameters={"type": "object", "properties": {}},
        handler=_noop,
        risk_level=risk,
        requires_approval=risk in (ToolRiskLevel.HIGH, ToolRiskLevel.CRITICAL),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestApprovalRequest:
    """审批请求创建"""

    @pytest.mark.asyncio
    async def test_request_blocks_until_resolved(self):
        """request_approval 应该阻塞直到 resolve 被调用"""
        engine = ApprovalEngine(default_timeout=10)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        async def approve_after_delay():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            assert len(pending) == 1
            req = pending[0]
            await engine.resolve(req.approval_id, approved=True)

        # 并发: 一边等审批，一边审批
        approve_task = asyncio.create_task(approve_after_delay())
        result = await engine.request_approval(call, tool_def, run_id="run-1")
        await approve_task

        assert result.approved is True
        assert result.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_request_rejected(self):
        """拒绝审批"""
        engine = ApprovalEngine(default_timeout=10)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        async def reject_after_delay():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            req = pending[0]
            await engine.resolve(
                req.approval_id, approved=False, comment="太危险了",
            )

        reject_task = asyncio.create_task(reject_after_delay())
        result = await engine.request_approval(call, tool_def, run_id="run-1")
        await reject_task

        assert result.approved is False
        assert result.status == ApprovalStatus.REJECTED
        assert result.comment == "太危险了"

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        """超时自动拒绝"""
        engine = ApprovalEngine(default_timeout=0.2)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        result = await engine.request_approval(call, tool_def, run_id="run-1")
        assert result.approved is False
        assert result.status == ApprovalStatus.TIMEOUT


class TestBatchApproval:
    """批量审批测试"""

    @pytest.mark.asyncio
    async def test_allow_remaining_in_run(self):
        """批量授权后同一 Run 的后续调用自动通过"""
        engine = ApprovalEngine(default_timeout=10)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        # 第一次: 手动审批并允许后续
        async def approve_all():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            req = pending[0]
            await engine.resolve(
                req.approval_id, approved=True, allow_remaining=True,
            )

        approve_task = asyncio.create_task(approve_all())
        result1 = await engine.request_approval(call, tool_def, run_id="run-1")
        await approve_task
        assert result1.approved is True
        assert result1.allow_remaining_in_run is True

        # 第二次: 自动通过
        result2 = await engine.request_approval(call, tool_def, run_id="run-1")
        assert result2.approved is True
        assert result2.status == ApprovalStatus.AUTO_APPROVED

    @pytest.mark.asyncio
    async def test_batch_approval_scoped_to_run(self):
        """批量授权限定在特定 Run 内"""
        engine = ApprovalEngine(default_timeout=10)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        # Run-1 获得批量授权
        async def approve_all():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            req = pending[0]
            await engine.resolve(
                req.approval_id, approved=True, allow_remaining=True,
            )

        task = asyncio.create_task(approve_all())
        await engine.request_approval(call, tool_def, run_id="run-1")
        await task

        # Run-2 不受影响，仍需审批
        async def approve_run2():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            assert len(pending) == 1  # 应有待审批请求
            req = pending[0]
            await engine.resolve(req.approval_id, approved=True)

        task2 = asyncio.create_task(approve_run2())
        result = await engine.request_approval(call, tool_def, run_id="run-2")
        await task2
        # 不是自动通过
        assert result.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_clear_run_cache(self):
        """清除 Run 缓存后需要重新审批"""
        engine = ApprovalEngine(default_timeout=10)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        # 建立批量授权
        async def approve():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            await engine.resolve(
                pending[0].approval_id, approved=True, allow_remaining=True,
            )

        task = asyncio.create_task(approve())
        await engine.request_approval(call, tool_def, run_id="run-1")
        await task

        # 清除缓存
        engine.clear_run_cache("run-1")

        # 需要重新审批
        async def approve_again():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            assert len(pending) == 1
            await engine.resolve(pending[0].approval_id, approved=True)

        task2 = asyncio.create_task(approve_again())
        result = await engine.request_approval(call, tool_def, run_id="run-1")
        await task2
        assert result.status == ApprovalStatus.APPROVED  # 非 AUTO_APPROVED


class TestNotifier:
    """通知回调测试"""

    @pytest.mark.asyncio
    async def test_notifier_called(self):
        """审批请求时应调用 notifier"""
        notified = []

        async def mock_notifier(request: ApprovalRequest):
            notified.append(request)

        engine = ApprovalEngine(default_timeout=10, notifier=mock_notifier)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        async def approve():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            await engine.resolve(pending[0].approval_id, approved=True)

        task = asyncio.create_task(approve())
        await engine.request_approval(call, tool_def, run_id="run-1")
        await task

        assert len(notified) == 1
        assert notified[0].tool_name == "system_run"

    @pytest.mark.asyncio
    async def test_notifier_error_does_not_block(self):
        """notifier 失败不应阻断审批流程"""
        async def bad_notifier(request):
            raise RuntimeError("通知发送失败")

        engine = ApprovalEngine(default_timeout=0.3, notifier=bad_notifier)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        # 超时仍然正常工作
        result = await engine.request_approval(call, tool_def, run_id="run-1")
        assert result.status == ApprovalStatus.TIMEOUT


class TestApprovalCard:
    """审批卡片生成测试"""

    def test_to_approval_card(self):
        engine = ApprovalEngine()
        request = ApprovalRequest(
            approval_id="test-123",
            run_id="run-1",
            tool_name="system_run",
            tool_description="执行 shell 命令",
            arguments={"command": "rm -rf /tmp/test"},
            risk_level="high",
            reason="工具 'system_run' 风险等级为 high",
            requested_at=1000000.0,
        )
        card = engine.to_approval_card(request)
        assert card["type"] == "approval_card"
        assert card["approval_id"] == "test-123"
        assert "[HIGH]" in card["title"]
        assert len(card["actions"]) == 3

    def test_card_truncates_long_arguments(self):
        engine = ApprovalEngine()
        request = ApprovalRequest(
            approval_id="test-456",
            run_id="run-1",
            tool_name="write_local_file",
            tool_description="写入文件",
            arguments={"content": "x" * 500},
            risk_level="medium",
            reason="测试",
            requested_at=1000000.0,
        )
        card = engine.to_approval_card(request)
        assert len(card["arguments"]["content"]) <= 210  # 200 + "..."


class TestResolveEdgeCases:
    """resolve 边界情况"""

    @pytest.mark.asyncio
    async def test_resolve_unknown_id(self):
        engine = ApprovalEngine()
        result = await engine.resolve("nonexistent", approved=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_clear_all(self):
        """clear_all 应清除所有状态"""
        engine = ApprovalEngine(default_timeout=10)
        call = _make_tool_call()
        tool_def = _make_tool_def()

        # 添加批量授权
        async def approve():
            await asyncio.sleep(0.1)
            pending = engine.list_pending()
            await engine.resolve(
                pending[0].approval_id, approved=True, allow_remaining=True,
            )

        task = asyncio.create_task(approve())
        await engine.request_approval(call, tool_def, run_id="run-1")
        await task

        engine.clear_all()
        assert engine.pending_count == 0
        assert len(engine._auto_approved) == 0
