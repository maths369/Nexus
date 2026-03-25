"""Async task manager — fire-and-forget dispatch with MQTT result listener.

This replaces the synchronous RPC pattern in dispatch_to_edge().
Hub publishes task via MQTT and returns immediately.
A background MQTT listener receives ack/progress/result and updates TaskStore.
EventSources (Desktop, Feishu, etc.) get notified through registered callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from .task_protocol import TaskAssignment, task_assign_topic, task_result_topic, task_status_topic
from .task_store import Task, TaskEvent, TaskStatus, TaskStore
from .transport import MeshMessage, MeshTransport, MessageType

logger = logging.getLogger(__name__)

# Callback type: receives a TaskEvent whenever a task's state changes
TaskEventCallback = Callable[[TaskEvent], Awaitable[None]]


class TaskManager:
    """Manages async task lifecycle across mesh nodes.

    Key responsibilities:
    - Submit tasks (fire-and-forget MQTT publish)
    - Listen for ack/progress/result from edge nodes
    - Update TaskStore
    - Notify registered EventSource callbacks
    - Monitor for timeouts and stale tasks
    """

    def __init__(
        self,
        *,
        transport: MeshTransport,
        local_node_id: str,
        ack_timeout_seconds: float = 30.0,
        heartbeat_timeout_seconds: float = 45.0,
        db_path: str | None = None,
    ) -> None:
        self._transport = transport
        self._local_node_id = local_node_id
        self._store = TaskStore(db_path=db_path)
        self._ack_timeout = ack_timeout_seconds
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._callbacks: dict[str, list[TaskEventCallback]] = {}  # task_id -> callbacks
        self._global_callbacks: list[TaskEventCallback] = []
        self._monitor_task: asyncio.Task[None] | None = None
        self._subscribed_topics: set[str] = set()
        self._last_heartbeat: dict[str, float] = {}  # task_id -> last status update time

    @property
    def store(self) -> TaskStore:
        return self._store

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background monitor for timeouts."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("TaskManager started (node=%s)", self._local_node_id)

    async def stop(self) -> None:
        """Stop the background monitor."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        # Unsubscribe all
        for topic in list(self._subscribed_topics):
            try:
                await self._transport.unsubscribe(topic)
            except Exception:
                pass
        self._subscribed_topics.clear()
        logger.info("TaskManager stopped")

    # ── Submit / Dispatch ───────────────────────────────────────

    async def submit_task(
        self,
        *,
        session_id: str,
        source_type: str,
        source_id: str,
        target_node: str,
        task_description: str,
        constraints: dict[str, Any] | None = None,
        timeout_seconds: float = 600.0,
        on_event: TaskEventCallback | None = None,
    ) -> Task:
        """Submit a task to a remote node — returns immediately after MQTT publish.

        Args:
            session_id: Session this task belongs to
            source_type: EventSource type ("desktop", "feishu", etc.)
            source_id: Unique EventSource instance ID
            target_node: Target mesh node ID
            task_description: What to do (natural language)
            constraints: Optional execution constraints
            timeout_seconds: Overall timeout for the task
            on_event: Callback for task events (progress, completion, etc.)

        Returns:
            Task object with task_id — check task.status for updates
        """
        task = self._store.create(
            session_id=session_id,
            source_type=source_type,
            source_id=source_id,
            gateway_node=self._local_node_id,
            task_description=task_description,
            executor_node=target_node,
            timeout_seconds=timeout_seconds,
        )

        # Register callback
        if on_event:
            self._callbacks.setdefault(task.task_id, []).append(on_event)

        # Subscribe to status and result topics for this task
        status_topic = task_status_topic(task.task_id)
        result_topic = task_result_topic(task.task_id)

        await self._transport.subscribe(status_topic, self._on_task_status)
        await self._transport.subscribe(result_topic, self._on_task_result)
        self._subscribed_topics.add(status_topic)
        self._subscribed_topics.add(result_topic)

        # Build and publish the TaskAssignment
        assignment = TaskAssignment(
            task_id=task.task_id,
            step_id="dispatch-step-1",
            assigned_node=target_node,
            tool_name="agent_loop",
            timeout_seconds=timeout_seconds,
            metadata={
                "execution_mode": "agent_loop",
                "task_description": task_description,
                "constraints": constraints or {},
                "source_type": source_type,
                "source_id": source_id,
                "session_id": session_id,
            },
        )

        assign_topic = task_assign_topic(task.task_id)
        msg = self._transport.make_message(
            MessageType.TASK_ASSIGN,
            assign_topic,
            assignment.to_dict(),
            target_node=target_node,
        )
        await self._transport.publish(assign_topic, msg)

        # Update status to DISPATCHED
        event = self._store.update_status(task.task_id, TaskStatus.DISPATCHED)
        if event:
            await self._notify(task.task_id, event)

        logger.info(
            "Task %s dispatched to %s (session=%s, source=%s)",
            task.task_id, target_node, session_id, source_type,
        )
        return task

    # ── Event registration ──────────────────────────────────────

    def on_task_event(self, task_id: str, callback: TaskEventCallback) -> None:
        """Register a callback for events on a specific task."""
        self._callbacks.setdefault(task_id, []).append(callback)

    def on_any_task_event(self, callback: TaskEventCallback) -> None:
        """Register a callback for events on ALL tasks."""
        self._global_callbacks.append(callback)

    # ── Wait for completion (optional, for backward compat) ─────

    async def wait_for_result(self, task_id: str, *, timeout: float = 600.0) -> Task | None:
        """Optionally wait for a task to complete. Use sparingly — prefer callbacks."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self._store.get(task_id)
            if task and task.status.is_terminal:
                return task
            await asyncio.sleep(1.0)
        return self._store.get(task_id)

    # ── MQTT Handlers ───────────────────────────────────────────

    async def _on_task_status(self, topic: str, message: MeshMessage) -> None:
        """Handle task status updates from edge nodes."""
        if message.message_type != MessageType.TASK_STATUS:
            return

        payload = message.payload
        task_id = str(payload.get("task_id", ""))
        status_str = str(payload.get("status", ""))
        node_id = str(payload.get("node_id", ""))
        error = payload.get("error")

        # Map edge TaskStepState to our TaskStatus
        status_map = {
            "queued": TaskStatus.ACKNOWLEDGED,
            "running": TaskStatus.EXECUTING,
            "waiting_approval": TaskStatus.ACKNOWLEDGED,
            "succeeded": TaskStatus.COMPLETED,
            "failed": TaskStatus.FAILED,
        }
        status = status_map.get(status_str)
        if not status:
            logger.warning("Unknown task status: %s for task %s", status_str, task_id)
            return

        progress_msg = str(payload.get("progress_message", ""))
        progress = payload.get("progress")

        event = self._store.update_status(
            task_id,
            status,
            executor_node=node_id or None,
            progress=int(progress) if progress is not None else None,
            progress_message=progress_msg or None,
            error=str(error) if error else None,
        )
        if event:
            self._last_heartbeat[task_id] = time.time()
            await self._notify(task_id, event)
            logger.debug("Task %s status -> %s (from %s)", task_id, status.value, node_id)

    async def _on_task_result(self, topic: str, message: MeshMessage) -> None:
        """Handle task result from edge nodes."""
        if message.message_type != MessageType.TASK_RESULT:
            return

        payload = message.payload
        task_id = str(payload.get("task_id", ""))
        success = bool(payload.get("success"))
        output = str(payload.get("output", ""))
        error = payload.get("error")
        node_id = str(payload.get("node_id", ""))

        status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
        event = self._store.update_status(
            task_id,
            status,
            result=output if success else None,
            error=str(error) if error else None,
            executor_node=node_id or None,
            progress=100 if success else None,
        )
        if event:
            await self._notify(task_id, event)
            logger.info("Task %s %s (from %s)", task_id, status.value, node_id)

        # Cleanup subscriptions for completed tasks
        await self._cleanup_task_subscriptions(task_id)

    # ── Notifications ───────────────────────────────────────────

    async def _notify(self, task_id: str, event: TaskEvent) -> None:
        """Push event to all registered callbacks."""
        callbacks = list(self._callbacks.get(task_id, []))
        callbacks.extend(self._global_callbacks)
        for cb in callbacks:
            try:
                await cb(event)
            except Exception as exc:
                logger.error("Task event callback error: %s", exc, exc_info=True)

        # Clean up callbacks and heartbeat tracking for terminal events
        if event.event_type in ("completed", "failed", "timed_out", "rejected", "stale"):
            self._callbacks.pop(task_id, None)
            self._last_heartbeat.pop(task_id, None)

    # ── Timeout Monitor ─────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Background loop: check for timed-out and stale tasks."""
        while True:
            try:
                await asyncio.sleep(10.0)
                now = time.time()
                for task in self._store.get_active():
                    # Check ACK timeout
                    if task.status == TaskStatus.DISPATCHED:
                        elapsed = now - (task.dispatched_at or task.created_at)
                        if elapsed > self._ack_timeout:
                            event = self._store.update_status(
                                task.task_id,
                                TaskStatus.TIMED_OUT,
                                error=f"节点 {task.executor_node} 未在 {self._ack_timeout}s 内确认",
                            )
                            if event:
                                await self._notify(task.task_id, event)
                            await self._cleanup_task_subscriptions(task.task_id)

                    # Check overall timeout + STALE heartbeat
                    elif task.status in (TaskStatus.ACKNOWLEDGED, TaskStatus.EXECUTING):
                        elapsed = now - task.created_at
                        if elapsed > task.timeout_seconds:
                            event = self._store.update_status(
                                task.task_id,
                                TaskStatus.TIMED_OUT,
                                error=f"任务总超时 ({task.timeout_seconds}s)",
                            )
                            if event:
                                await self._notify(task.task_id, event)
                            await self._cleanup_task_subscriptions(task.task_id)
                            self._last_heartbeat.pop(task.task_id, None)

                        # STALE heartbeat: EXECUTING but no status update for 2× heartbeat_timeout
                        elif task.status == TaskStatus.EXECUTING:
                            last_hb = self._last_heartbeat.get(task.task_id, task.created_at)
                            if now - last_hb > 2 * self._heartbeat_timeout:
                                event = self._store.update_status(
                                    task.task_id,
                                    TaskStatus.STALE,
                                    error=f"任务心跳超时 (>{2 * self._heartbeat_timeout:.0f}s 未收到状态更新)",
                                )
                                if event:
                                    await self._notify(task.task_id, event)
                                await self._cleanup_task_subscriptions(task.task_id)
                                self._last_heartbeat.pop(task.task_id, None)

                # Periodic cleanup of old completed tasks
                self._store.cleanup_old(max_age_seconds=3600.0)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("TaskManager monitor error: %s", exc, exc_info=True)

    async def _cleanup_task_subscriptions(self, task_id: str) -> None:
        """Unsubscribe from MQTT topics for a completed task."""
        for topic_fn in (task_status_topic, task_result_topic):
            topic = topic_fn(task_id)
            if topic in self._subscribed_topics:
                try:
                    await self._transport.unsubscribe(topic)
                except Exception:
                    pass
                self._subscribed_topics.discard(topic)
