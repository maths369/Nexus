"""EventSource abstraction layer for pushing task results back to user-facing channels.

EventSources represent user-facing channels (Desktop UI, Feishu, etc.) — NOT
execution nodes.  When an async task completes on a remote mesh node, the result
is pushed back to the originating EventSource identified by (source_type, source_id).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from nexus.mesh.task_store import TaskEvent

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class EventSource(ABC):
    """Base class for all user-facing event channels."""

    source_type: str  # set by each concrete subclass at the class level

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id

    @abstractmethod
    async def push_event(self, event: TaskEvent) -> None:
        """Push a generic task lifecycle event to the channel."""

    @abstractmethod
    async def push_result(self, task_id: str, result: str) -> None:
        """Push a completed-task result to the channel."""

    @abstractmethod
    async def push_error(self, task_id: str, error: str) -> None:
        """Push a task error to the channel."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.source_type}:{self.source_id}>"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class EventSourceRegistry:
    """Thread-safe registry mapping (source_type, source_id) to EventSource instances."""

    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], EventSource] = {}

    def register(self, source: EventSource) -> None:
        key = (source.source_type, source.source_id)
        self._sources[key] = source
        log.info("Registered EventSource %s", source)

    def unregister(self, source_type: str, source_id: str) -> None:
        key = (source_type, source_id)
        removed = self._sources.pop(key, None)
        if removed:
            log.info("Unregistered EventSource %s", removed)
        else:
            log.warning("Attempted to unregister unknown EventSource %s:%s", source_type, source_id)

    def get(self, source_type: str, source_id: str) -> EventSource | None:
        return self._sources.get((source_type, source_id))

    def get_all(self, source_type: str) -> list[EventSource]:
        return [s for (st, _), s in self._sources.items() if st == source_type]

    async def push_to_source(self, source_type: str, source_id: str, event: TaskEvent) -> bool:
        """Push an event to a specific source. Returns False if source not found."""
        source = self.get(source_type, source_id)
        if source is None:
            log.warning("push_to_source: no EventSource for %s:%s", source_type, source_id)
            return False
        try:
            await source.push_event(event)
            return True
        except Exception:
            log.exception("Failed to push event to %s", source)
            return False


# ---------------------------------------------------------------------------
# Concrete: Feishu
# ---------------------------------------------------------------------------


class FeishuEventSource(EventSource):
    """EventSource backed by a Feishu (Lark) chat channel."""

    source_type: str = "feishu"

    def __init__(self, chat_id: str, send_fn: Callable[[str], Awaitable[None]]) -> None:
        super().__init__(source_id=chat_id)
        self._send_fn = send_fn

    @property
    def chat_id(self) -> str:
        return self.source_id

    async def push_event(self, event: TaskEvent) -> None:
        text = f"[{event.event_type}] Task {event.task_id}: {event.content}"
        if event.progress is not None:
            text += f" ({event.progress}%)"
        log.debug("Feishu push_event to %s: %s", self.chat_id, text)
        await self._send_fn(text)

    async def push_result(self, task_id: str, result: str) -> None:
        text = f"Task {task_id} completed:\n{result}"
        log.debug("Feishu push_result to %s: task=%s", self.chat_id, task_id)
        await self._send_fn(text)

    async def push_error(self, task_id: str, error: str) -> None:
        text = f"Task {task_id} failed:\n{error}"
        log.debug("Feishu push_error to %s: task=%s", self.chat_id, task_id)
        await self._send_fn(text)


# ---------------------------------------------------------------------------
# Concrete: Desktop (SSE queue)
# ---------------------------------------------------------------------------


class DesktopEventSource(EventSource):
    """EventSource backed by an asyncio.Queue for SSE push to a desktop UI."""

    source_type: str = "desktop"

    def __init__(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        super().__init__(source_id=session_id)
        self._queue = queue

    @property
    def session_id(self) -> str:
        return self.source_id

    async def push_event(self, event: TaskEvent) -> None:
        payload = {"type": "task_event", **event.to_dict()}
        log.debug("Desktop push_event to %s: %s", self.session_id, event.event_type)
        await self._queue.put(payload)

    async def push_result(self, task_id: str, result: str) -> None:
        payload = {"type": "task_result", "task_id": task_id, "result": result}
        log.debug("Desktop push_result to %s: task=%s", self.session_id, task_id)
        await self._queue.put(payload)

    async def push_error(self, task_id: str, error: str) -> None:
        payload = {"type": "task_error", "task_id": task_id, "error": error}
        log.debug("Desktop push_error to %s: task=%s", self.session_id, task_id)
        await self._queue.put(payload)
