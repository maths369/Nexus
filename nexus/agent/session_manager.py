"""Session lifecycle control plane for long-running agent execution."""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore

logger = logging.getLogger(__name__)

RunCoroutine = Callable[[], Awaitable[None]]


class EnqueueResult(str, enum.Enum):
    """enqueue_run 的返回状态。"""
    STARTED = "started"    # 无等待，直接开始执行
    QUEUED = "queued"      # 已排队，等待前序任务完成
    FULL = "full"          # 队列已满，无法接受


class ManagedSessionState(str, enum.Enum):
    ACTIVE = "active"
    IDLE = "idle"
    SUSPENDED = "suspended"
    CLOSED = "closed"


@dataclass
class RuntimeEntry:
    session_id: str
    state: ManagedSessionState = ManagedSessionState.ACTIVE
    cached_at: datetime = field(default_factory=datetime.now)
    last_active_at: datetime = field(default_factory=datetime.now)
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None
    provider_name: str | None = None
    tool_profile: str | None = None
    tool_names: tuple[str, ...] = ()
    context_message_count: int = 0
    route_hint: str = "auto"
    suspended_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "cached_at": self.cached_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat(),
            "last_run_started_at": (
                self.last_run_started_at.isoformat()
                if self.last_run_started_at is not None
                else None
            ),
            "last_run_finished_at": (
                self.last_run_finished_at.isoformat()
                if self.last_run_finished_at is not None
                else None
            ),
            "provider_name": self.provider_name,
            "tool_profile": self.tool_profile,
            "tool_names": list(self.tool_names),
            "context_message_count": self.context_message_count,
            "route_hint": self.route_hint,
            "suspended_at": (
                self.suspended_at.isoformat()
                if self.suspended_at is not None
                else None
            ),
            **self.extra,
        }


@dataclass
class _SessionJob:
    run_coro: RunCoroutine
    future: asyncio.Future[None]


@dataclass
class _SessionWorker:
    session_id: str
    queue: asyncio.Queue[_SessionJob | None]
    task: asyncio.Task[None] | None = None
    running: bool = False


class SessionManager:
    """Coordinates per-session run serialization and runtime cache metadata."""

    def __init__(
        self,
        session_store: SessionStore,
        session_router: SessionRouter | None = None,
        *,
        idle_timeout_minutes: int = 30,
        max_concurrent_sessions: int = 20,
        sweep_interval_seconds: float = 60.0,
        max_queue_size: int = 8,
    ) -> None:
        self._store = session_store
        self._router = session_router
        self._idle_timeout = timedelta(minutes=max(1, int(idle_timeout_minutes or 30)))
        self._suspend_timeout = self._idle_timeout * 2
        self._sweep_interval_seconds = max(5.0, float(sweep_interval_seconds or 60.0))
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent_sessions or 20)))
        self._max_queue_size = max(1, int(max_queue_size or 8))
        self._runtime_cache: dict[str, RuntimeEntry] = {}
        self._workers: dict[str, _SessionWorker] = {}
        self._registry_lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._closed = False
        self._sweeper_task = asyncio.create_task(
            self._sweeper_loop(),
            name="session-manager-sweeper",
        )

    async def stop(self) -> None:
        self._closed = True
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.task.cancel()
        for worker in workers:
            try:
                await worker.task
            except asyncio.CancelledError:
                pass

    async def enqueue_run(
        self,
        session_id: str,
        run_coro: RunCoroutine,
    ) -> tuple[EnqueueResult, asyncio.Future[None] | None]:
        """将 run 排入 session 的串行队列。

        返回 (状态, future):
        - STARTED: 队列空闲，直接开始执行
        - QUEUED: 前面有任务在跑，已排队等待
        - FULL: 队列已满，无法接受

        调用方需自行 ``await future`` 等待执行完成。
        """
        if self._closed:
            raise RuntimeError("SessionManager is closed")
        async with self._registry_lock:
            worker = self._workers.get(session_id)
            if worker is None or worker.task.done():
                worker = self._create_worker(session_id)
            busy = worker.running or not worker.queue.empty()
            if busy and worker.queue.full():
                return EnqueueResult.FULL, None
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            await worker.queue.put(_SessionJob(run_coro=run_coro, future=future))
            self._set_state(session_id, ManagedSessionState.ACTIVE)
        return (EnqueueResult.QUEUED if busy else EnqueueResult.STARTED), future

    # ------------------------------------------------------------------
    # 查询方法
    # ------------------------------------------------------------------

    def is_session_busy(self, session_id: str) -> bool:
        """session 的 worker 是否正在执行或队列中有等待任务。"""
        worker = self._workers.get(session_id)
        if worker is None:
            return False
        return worker.running or not worker.queue.empty()

    def get_queue_depth(self, session_id: str) -> int:
        """session 队列中等待执行的任务数。"""
        worker = self._workers.get(session_id)
        if worker is None:
            return 0
        return worker.queue.qsize()

    def capture_runtime_snapshot(
        self,
        session_id: str,
        *,
        provider_name: str | None = None,
        context_message_count: int | None = None,
        tool_profile: str | None = None,
        tool_names: list[str] | tuple[str, ...] | None = None,
        route_hint: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry = self._get_or_create_entry(session_id)
        entry.cached_at = datetime.now()
        entry.last_active_at = entry.cached_at
        if provider_name is not None:
            entry.provider_name = provider_name
        if context_message_count is not None:
            entry.context_message_count = max(0, int(context_message_count))
        if tool_profile is not None:
            entry.tool_profile = tool_profile
        if tool_names is not None:
            entry.tool_names = tuple(str(name) for name in tool_names if str(name).strip())
        if route_hint is not None:
            entry.route_hint = str(route_hint)
        if extra:
            entry.extra.update(extra)
        self._persist_runtime_entry(session_id, entry)

    def get_runtime_entry(self, session_id: str) -> RuntimeEntry | None:
        return self._runtime_cache.get(session_id)

    async def sweep_once(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now()
        for session_id, entry in list(self._runtime_cache.items()):
            worker = self._workers.get(session_id)
            if worker is not None and (worker.running or not worker.queue.empty()):
                continue
            if entry.state == ManagedSessionState.CLOSED:
                continue
            idle_for = now - entry.last_active_at
            if idle_for >= self._suspend_timeout:
                if entry.state != ManagedSessionState.SUSPENDED:
                    entry.suspended_at = now
                    self._set_state(session_id, ManagedSessionState.SUSPENDED, entry=entry)
                continue
            if idle_for >= self._idle_timeout and entry.state == ManagedSessionState.ACTIVE:
                self._set_state(session_id, ManagedSessionState.IDLE, entry=entry)

    def close_session(self, session_id: str) -> None:
        entry = self._get_or_create_entry(session_id)
        self._set_state(session_id, ManagedSessionState.CLOSED, entry=entry)

    def _create_worker(self, session_id: str) -> _SessionWorker:
        queue: asyncio.Queue[_SessionJob | None] = asyncio.Queue(maxsize=self._max_queue_size)
        worker = _SessionWorker(session_id=session_id, queue=queue)
        self._workers[session_id] = worker
        task = asyncio.create_task(
            self._worker_loop(session_id, queue),
            name=f"session-worker-{session_id}",
        )
        worker.task = task
        return worker

    async def _worker_loop(
        self,
        session_id: str,
        queue: asyncio.Queue[_SessionJob | None],
    ) -> None:
        worker = self._workers.get(session_id)
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                return
            if worker is not None:
                worker.running = True
            entry = self._get_or_create_entry(session_id)
            started_at = datetime.now()
            entry.last_run_started_at = started_at
            entry.last_active_at = started_at
            entry.cached_at = started_at
            entry.state = ManagedSessionState.ACTIVE
            self._persist_runtime_entry(session_id, entry)
            try:
                async with self._semaphore:
                    await job.run_coro()
            except Exception as exc:  # noqa: BLE001
                if not job.future.done():
                    job.future.set_exception(exc)
            else:
                if not job.future.done():
                    job.future.set_result(None)
            finally:
                finished_at = datetime.now()
                entry.last_run_finished_at = finished_at
                entry.last_active_at = finished_at
                entry.cached_at = finished_at
                if worker is not None:
                    worker.running = False
                self._persist_runtime_entry(session_id, entry)
                queue.task_done()

    async def _sweeper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sweep_interval_seconds)
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("SessionManager sweeper loop failed", exc_info=True)

    def _get_or_create_entry(self, session_id: str) -> RuntimeEntry:
        entry = self._runtime_cache.get(session_id)
        if entry is None:
            entry = RuntimeEntry(session_id=session_id)
            self._runtime_cache[session_id] = entry
        return entry

    def _set_state(
        self,
        session_id: str,
        state: ManagedSessionState,
        *,
        entry: RuntimeEntry | None = None,
    ) -> None:
        target = entry or self._get_or_create_entry(session_id)
        target.state = state
        target.cached_at = datetime.now()
        if state == ManagedSessionState.ACTIVE:
            target.last_active_at = target.cached_at
            target.suspended_at = None
        if state == ManagedSessionState.SUSPENDED and target.suspended_at is None:
            target.suspended_at = target.cached_at
        self._persist_runtime_entry(session_id, target)

    def _persist_runtime_entry(self, session_id: str, entry: RuntimeEntry) -> None:
        self._runtime_cache[session_id] = entry
        try:
            self._store.update_session_metadata(
                session_id,
                {"session_runtime": entry.to_metadata()},
            )
        except KeyError:
            logger.debug("Skip persisting runtime cache for unknown session %s", session_id)
