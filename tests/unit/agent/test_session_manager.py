from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from nexus.agent.session_manager import EnqueueResult, ManagedSessionState, SessionManager
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore


# ---------------------------------------------------------------------------
# 排队模型测试
# ---------------------------------------------------------------------------

async def _run_with_queue(manager: SessionManager, session_id: str):
    """第一个 job 占住 worker，第二个 job 应排队而非被拒绝。"""
    entered = asyncio.Event()
    release = asyncio.Event()
    execution_order: list[str] = []

    async def slow_job() -> None:
        execution_order.append("slow_start")
        entered.set()
        await release.wait()
        execution_order.append("slow_end")

    async def fast_job() -> None:
        execution_order.append("fast")

    result1, future1 = await manager.enqueue_run(session_id, slow_job)
    await entered.wait()  # 等 slow_job 开始执行

    result2, future2 = await manager.enqueue_run(session_id, fast_job)

    release.set()  # 释放 slow_job
    if future1:
        await future1
    if future2:
        await future2

    return result1, result2, execution_order


def test_session_manager_queues_concurrent_run_for_same_session(tmp_path):
    """同 session 的第二个消息应排队等待，而非被拒绝。"""
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    manager = SessionManager(store, router, idle_timeout_minutes=1, max_concurrent_sessions=2)
    session = store.create_session("user-1", "feishu", summary="排队测试")

    result1, result2, order = asyncio.run(
        _run_with_queue(manager, session.session_id)
    )

    # 第一个直接执行，第二个排队
    assert result1 == EnqueueResult.STARTED
    assert result2 == EnqueueResult.QUEUED
    # 严格串行: slow 完成后 fast 才执行
    assert order == ["slow_start", "slow_end", "fast"]


def test_session_manager_returns_full_when_queue_is_saturated(tmp_path):
    """队列满时返回 FULL。"""
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    manager = SessionManager(
        store, router,
        idle_timeout_minutes=1,
        max_queue_size=2,  # 极小队列
    )
    session = store.create_session("user-1", "feishu", summary="队列满测试")

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            entered.set()
            await release.wait()

        # 第 1 个: 占住 worker
        r1, f1 = await manager.enqueue_run(session.session_id, blocker)
        await entered.wait()

        # 第 2、3 个: 填满队列 (maxsize=2)
        r2, f2 = await manager.enqueue_run(
            session.session_id, lambda: asyncio.sleep(0)
        )
        r3, f3 = await manager.enqueue_run(
            session.session_id, lambda: asyncio.sleep(0)
        )

        # 第 4 个: 队列已满
        r4, f4 = await manager.enqueue_run(
            session.session_id, lambda: asyncio.sleep(0)
        )

        release.set()
        for f in (f1, f2, f3):
            if f:
                await f

        return r1, r2, r3, r4

    r1, r2, r3, r4 = asyncio.run(scenario())
    assert r1 == EnqueueResult.STARTED
    assert r2 == EnqueueResult.QUEUED
    assert r3 == EnqueueResult.QUEUED
    assert r4 == EnqueueResult.FULL


def test_is_session_busy_and_queue_depth(tmp_path):
    """is_session_busy / get_queue_depth 正确反映运行状态。"""
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    manager = SessionManager(store, router, idle_timeout_minutes=1)
    session = store.create_session("user-1", "feishu", summary="状态查询")

    async def scenario():
        # 初始: 不繁忙
        assert not manager.is_session_busy(session.session_id)
        assert manager.get_queue_depth(session.session_id) == 0

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            entered.set()
            await release.wait()

        _, f1 = await manager.enqueue_run(session.session_id, blocker)
        await entered.wait()

        # worker 在跑: 繁忙
        assert manager.is_session_busy(session.session_id)
        assert manager.get_queue_depth(session.session_id) == 0

        # 再排一个
        _, f2 = await manager.enqueue_run(
            session.session_id, lambda: asyncio.sleep(0)
        )
        assert manager.get_queue_depth(session.session_id) == 1

        release.set()
        if f1:
            await f1
        if f2:
            await f2

        # 全部完成
        assert not manager.is_session_busy(session.session_id)
        assert manager.get_queue_depth(session.session_id) == 0

    asyncio.run(scenario())


def test_enqueue_run_returns_future_that_resolves(tmp_path):
    """future 在 job 完成后正确 resolve。"""
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    manager = SessionManager(store, router, idle_timeout_minutes=1)
    session = store.create_session("user-1", "feishu", summary="future 测试")

    executed = False

    async def job():
        nonlocal executed
        executed = True

    async def scenario():
        result, future = await manager.enqueue_run(session.session_id, job)
        assert result == EnqueueResult.STARTED
        assert future is not None
        await future
        assert executed

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 原有测试: sweep 逻辑
# ---------------------------------------------------------------------------

def test_session_manager_marks_idle_and_suspended_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    manager = SessionManager(store, router, idle_timeout_minutes=30)
    session = store.create_session("user-1", "feishu", summary="会话缓存")
    manager.capture_runtime_snapshot(session.session_id, provider_name="qwen-max")

    entry = manager.get_runtime_entry(session.session_id)
    assert entry is not None
    baseline = datetime.now()
    entry.last_active_at = baseline - timedelta(minutes=31)
    asyncio.run(manager.sweep_once(now=baseline))

    runtime = store.get_session(session.session_id).metadata["session_runtime"]
    assert runtime["state"] == ManagedSessionState.IDLE.value

    entry.last_active_at = baseline - timedelta(minutes=65)
    asyncio.run(manager.sweep_once(now=baseline))
    runtime = store.get_session(session.session_id).metadata["session_runtime"]
    assert runtime["state"] == ManagedSessionState.SUSPENDED.value
