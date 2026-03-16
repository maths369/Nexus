"""BackgroundTaskManager 测试 — 异步后台执行"""

from __future__ import annotations

import asyncio
import pytest

from nexus.agent.background import BackgroundTaskManager, MAX_BACKGROUND_TASKS


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _run(coro):
    """运行异步函数"""
    return asyncio.run(coro)


def _run_with_manager(mgr: BackgroundTaskManager, coro):
    async def wrapped():
        try:
            return await coro
        finally:
            await mgr.aclose()

    return asyncio.run(wrapped())


# ---------------------------------------------------------------------------
# 提交与执行
# ---------------------------------------------------------------------------

def test_submit_returns_task_id():
    """submit 立即返回包含 task_id 的消息"""
    mgr = BackgroundTaskManager()
    result = _run_with_manager(mgr, mgr.submit("echo hello"))
    assert "已启动" in result
    assert "echo hello" in result


def test_submit_and_wait_for_completion():
    """提交后等待完成"""
    mgr = BackgroundTaskManager()

    async def run_test():
        await mgr.submit("echo hello world")
        # 等待一小段时间让后台任务完成
        await asyncio.sleep(1)
        stats = mgr.stats
        assert stats["completed"] == 1
        assert stats["running"] == 0

    _run_with_manager(mgr, run_test())


def test_submit_failing_command():
    """提交失败命令"""
    mgr = BackgroundTaskManager()

    async def run_test():
        await mgr.submit("false")  # 'false' 命令总是返回非 0
        await asyncio.sleep(1)
        stats = mgr.stats
        assert stats["completed"] == 1  # error 也算 completed
        # 检查状态为 error
        for task in mgr._tasks.values():
            assert task["status"] == "error"

    _run_with_manager(mgr, run_test())


def test_submit_timeout():
    """超时任务被终止"""
    mgr = BackgroundTaskManager(timeout=1)

    async def run_test():
        await mgr.submit("sleep 60")  # 会超时
        await asyncio.sleep(2)
        stats = mgr.stats
        assert stats["completed"] == 1
        for task in mgr._tasks.values():
            assert task["status"] == "timeout"

    _run_with_manager(mgr, run_test())


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------

def test_check_no_tasks():
    """没有任务时的查询"""
    mgr = BackgroundTaskManager()
    try:
        assert "无后台任务" in mgr.check()
    finally:
        _run(mgr.aclose())


def test_check_specific_task():
    """查询特定任务"""
    mgr = BackgroundTaskManager()

    async def run_test():
        result = await mgr.submit("echo hi")
        # 从返回消息中提取 task_id
        task_id = list(mgr._tasks.keys())[0]
        await asyncio.sleep(1)
        status = mgr.check(task_id)
        assert "completed" in status or "hi" in status

    _run_with_manager(mgr, run_test())


def test_check_unknown_task():
    """查询不存在的任务"""
    mgr = BackgroundTaskManager()
    try:
        result = mgr.check("nonexistent")
        assert "Error" in result
    finally:
        _run(mgr.aclose())


def test_check_list_all():
    """列出所有任务"""
    mgr = BackgroundTaskManager()

    async def run_test():
        await mgr.submit("echo a")
        await mgr.submit("echo b")
        await asyncio.sleep(1)
        listing = mgr.check()
        assert "echo a" in listing
        assert "echo b" in listing

    _run_with_manager(mgr, run_test())


# ---------------------------------------------------------------------------
# 通知队列
# ---------------------------------------------------------------------------

def test_drain_notifications():
    """drain_notifications 返回已完成通知并清空队列"""
    mgr = BackgroundTaskManager()

    async def run_test():
        await mgr.submit("echo notification_test")
        await asyncio.sleep(1)

        notifs = await mgr.drain_notifications()
        assert len(notifs) == 1
        assert notifs[0]["status"] == "completed"
        assert "notification_test" in notifs[0]["result"]

        # 第二次 drain 应为空
        notifs2 = await mgr.drain_notifications()
        assert len(notifs2) == 0

    _run(run_test())


def test_format_notifications():
    """格式化通知为可注入消息"""
    mgr = BackgroundTaskManager()
    try:
        notifs = [
            {"task_id": "abc", "status": "completed", "result": "结果A"},
            {"task_id": "def", "status": "error", "result": "错误B"},
        ]
        formatted = mgr.format_notifications(notifs)
        assert "<background-results>" in formatted
        assert "</background-results>" in formatted
        assert "[bg:abc]" in formatted
        assert "[bg:def]" in formatted
    finally:
        _run(mgr.aclose())


def test_drain_empty():
    """空队列 drain"""
    mgr = BackgroundTaskManager()

    async def run_test():
        notifs = await mgr.drain_notifications()
        assert notifs == []

    _run_with_manager(mgr, run_test())


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

def test_clear_completed():
    """清理已完成的任务记录"""
    mgr = BackgroundTaskManager()

    async def run_test():
        await mgr.submit("echo x")
        await mgr.submit("echo y")
        await asyncio.sleep(1)

        assert mgr.stats["total"] == 2
        result = mgr.clear_completed()
        assert "2" in result
        assert mgr.stats["total"] == 0

    _run_with_manager(mgr, run_test())


# ---------------------------------------------------------------------------
# 并发限制
# ---------------------------------------------------------------------------

def test_max_tasks_limit():
    """达到并发上限时拒绝新任务"""
    mgr = BackgroundTaskManager()

    async def run_test():
        # 用 sleep 命令填满队列
        for _ in range(MAX_BACKGROUND_TASKS):
            await mgr.submit("sleep 30")

        # 下一个应该被拒绝
        result = await mgr.submit("echo overflow")
        assert "上限" in result

    _run_with_manager(mgr, run_test())


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_stats():
    """统计数据正确"""
    mgr = BackgroundTaskManager()

    stats = mgr.stats
    assert stats["running"] == 0
    assert stats["completed"] == 0
    assert stats["total"] == 0

    async def run_test():
        await mgr.submit("echo a")
        await asyncio.sleep(1)
        stats = mgr.stats
        assert stats["completed"] == 1
        assert stats["total"] == 1

    _run_with_manager(mgr, run_test())
