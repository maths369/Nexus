"""Async task store — tracks task lifecycle across mesh nodes."""

from __future__ import annotations

import enum
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class TaskStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STALE = "stale"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
        )


@dataclass
class TaskEvent:
    """A single event in a task's lifecycle, pushed to EventSources."""

    task_id: str
    event_type: str  # "dispatched" | "acknowledged" | "progress" | "completed" | "failed"
    content: str  # Human-readable text for display
    progress: int | None = None  # 0-100, only for progress events
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "event_type": self.event_type,
            "content": self.content,
            "progress": self.progress,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


@dataclass
class Task:
    """A mesh task with full lifecycle tracking."""

    task_id: str
    session_id: str
    source_type: str  # "desktop" | "feishu" | "api"
    source_id: str  # unique identifier for the EventSource instance
    gateway_node: str  # node that received the request
    task_description: str

    executor_node: str | None = None
    status: TaskStatus = TaskStatus.SUBMITTED
    progress: int = 0
    progress_message: str = ""
    result: str | None = None
    error: str | None = None
    attempt: int = 0
    max_retries: int = 1
    timeout_seconds: float = 600.0

    created_at: float = field(default_factory=time.time)
    dispatched_at: float | None = None
    acknowledged_at: float | None = None
    completed_at: float | None = None

    events: list[TaskEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "gateway_node": self.gateway_node,
            "executor_node": self.executor_node,
            "task_description": self.task_description,
            "status": self.status.value,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "result": self.result,
            "error": self.error,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
            "acknowledged_at": self.acknowledged_at,
            "completed_at": self.completed_at,
        }


class TaskStore:
    """Task store with event tracking and optional SQLite persistence.

    All tasks are indexed by task_id and session_id for efficient lookups.
    When *db_path* is provided, tasks are persisted to SQLite so that active
    tasks survive Hub restarts.  Without *db_path* the store is purely
    in-memory (backward-compatible).
    """

    _CREATE_TABLE = """\
    CREATE TABLE IF NOT EXISTS tasks (
        task_id          TEXT PRIMARY KEY,
        session_id       TEXT NOT NULL,
        source_type      TEXT NOT NULL,
        source_id        TEXT NOT NULL,
        gateway_node     TEXT NOT NULL,
        task_description TEXT NOT NULL,
        executor_node    TEXT,
        status           TEXT NOT NULL,
        progress         INTEGER NOT NULL DEFAULT 0,
        progress_message TEXT NOT NULL DEFAULT '',
        result           TEXT,
        error            TEXT,
        attempt          INTEGER NOT NULL DEFAULT 0,
        max_retries      INTEGER NOT NULL DEFAULT 1,
        timeout_seconds  REAL NOT NULL DEFAULT 600.0,
        created_at       REAL NOT NULL,
        dispatched_at    REAL,
        acknowledged_at  REAL,
        completed_at     REAL,
        events_json      TEXT NOT NULL DEFAULT '[]'
    );
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._tasks: dict[str, Task] = {}
        self._by_session: dict[str, list[str]] = {}
        self._db: sqlite3.Connection | None = None

        if db_path is not None:
            self._init_db(db_path)
            self._load_from_db()

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------

    def _init_db(self, db_path: str) -> None:
        """Open (or create) the SQLite database and ensure the table exists."""
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(self._CREATE_TABLE)
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )
        self._db.commit()
        log.info("TaskStore SQLite opened: %s", db_path)

    def _load_from_db(self) -> None:
        """Restore non-terminal tasks from SQLite into memory."""
        if self._db is None:
            return

        terminal = {s.value for s in TaskStatus if s.is_terminal}
        cur = self._db.execute(
            "SELECT * FROM tasks WHERE status NOT IN ({})".format(
                ",".join("?" for _ in terminal)
            ),
            list(terminal),
        )
        cols = [d[0] for d in cur.description]
        count = 0
        for row in cur.fetchall():
            rd = dict(zip(cols, row))
            events_raw: list[dict[str, Any]] = json.loads(rd.pop("events_json"))
            events = [
                TaskEvent(
                    task_id=e["task_id"],
                    event_type=e["event_type"],
                    content=e["content"],
                    progress=e.get("progress"),
                    metadata=e.get("metadata", {}),
                    timestamp=e.get("timestamp", 0.0),
                )
                for e in events_raw
            ]
            task = Task(
                task_id=rd["task_id"],
                session_id=rd["session_id"],
                source_type=rd["source_type"],
                source_id=rd["source_id"],
                gateway_node=rd["gateway_node"],
                task_description=rd["task_description"],
                executor_node=rd["executor_node"],
                status=TaskStatus(rd["status"]),
                progress=rd["progress"],
                progress_message=rd["progress_message"],
                result=rd["result"],
                error=rd["error"],
                attempt=rd["attempt"],
                max_retries=rd["max_retries"],
                timeout_seconds=rd["timeout_seconds"],
                created_at=rd["created_at"],
                dispatched_at=rd["dispatched_at"],
                acknowledged_at=rd["acknowledged_at"],
                completed_at=rd["completed_at"],
                events=events,
            )
            self._tasks[task.task_id] = task
            self._by_session.setdefault(task.session_id, []).append(task.task_id)
            count += 1

        if count:
            log.info("TaskStore restored %d active task(s) from SQLite", count)

    def _db_insert(self, task: Task) -> None:
        """Insert a task row into SQLite."""
        if self._db is None:
            return
        try:
            self._db.execute(
                """\
                INSERT INTO tasks (
                    task_id, session_id, source_type, source_id,
                    gateway_node, task_description, executor_node,
                    status, progress, progress_message,
                    result, error, attempt, max_retries, timeout_seconds,
                    created_at, dispatched_at, acknowledged_at, completed_at,
                    events_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task.task_id, task.session_id, task.source_type, task.source_id,
                    task.gateway_node, task.task_description, task.executor_node,
                    task.status.value, task.progress, task.progress_message,
                    task.result, task.error, task.attempt, task.max_retries,
                    task.timeout_seconds,
                    task.created_at, task.dispatched_at, task.acknowledged_at,
                    task.completed_at,
                    json.dumps([e.to_dict() for e in task.events]),
                ),
            )
            self._db.commit()
        except Exception:
            log.exception("TaskStore failed to insert task %s", task.task_id)

    def _db_update(self, task: Task) -> None:
        """Update an existing task row in SQLite."""
        if self._db is None:
            return
        try:
            self._db.execute(
                """\
                UPDATE tasks SET
                    executor_node = ?,
                    status = ?,
                    progress = ?,
                    progress_message = ?,
                    result = ?,
                    error = ?,
                    attempt = ?,
                    dispatched_at = ?,
                    acknowledged_at = ?,
                    completed_at = ?,
                    events_json = ?
                WHERE task_id = ?
                """,
                (
                    task.executor_node,
                    task.status.value,
                    task.progress,
                    task.progress_message,
                    task.result,
                    task.error,
                    task.attempt,
                    task.dispatched_at,
                    task.acknowledged_at,
                    task.completed_at,
                    json.dumps([e.to_dict() for e in task.events]),
                    task.task_id,
                ),
            )
            self._db.commit()
        except Exception:
            log.exception("TaskStore failed to update task %s", task.task_id)

    def _db_delete(self, task_ids: list[str]) -> None:
        """Delete task rows from SQLite."""
        if self._db is None or not task_ids:
            return
        try:
            placeholders = ",".join("?" for _ in task_ids)
            self._db.execute(
                f"DELETE FROM tasks WHERE task_id IN ({placeholders})",
                task_ids,
            )
            self._db.commit()
        except Exception:
            log.exception("TaskStore failed to delete %d task(s)", len(task_ids))

    # ------------------------------------------------------------------
    # Public API (unchanged signatures)
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        session_id: str,
        source_type: str,
        source_id: str,
        gateway_node: str,
        task_description: str,
        executor_node: str | None = None,
        timeout_seconds: float = 600.0,
    ) -> Task:
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        task = Task(
            task_id=task_id,
            session_id=session_id,
            source_type=source_type,
            source_id=source_id,
            gateway_node=gateway_node,
            task_description=task_description,
            executor_node=executor_node,
            timeout_seconds=timeout_seconds,
        )
        self._tasks[task_id] = task
        self._by_session.setdefault(session_id, []).append(task_id)
        self._db_insert(task)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def get_by_session(self, session_id: str) -> list[Task]:
        task_ids = self._by_session.get(session_id, [])
        return [self._tasks[tid] for tid in task_ids if tid in self._tasks]

    def get_active(self) -> list[Task]:
        return [t for t in self._tasks.values() if not t.status.is_terminal]

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        progress: int | None = None,
        progress_message: str | None = None,
        result: str | None = None,
        error: str | None = None,
        executor_node: str | None = None,
    ) -> TaskEvent | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.status.is_terminal:
            return None  # Don't update terminal tasks (idempotent)

        task.status = status
        if progress is not None:
            task.progress = progress
        if progress_message is not None:
            task.progress_message = progress_message
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if executor_node is not None:
            task.executor_node = executor_node

        now = time.time()
        if status == TaskStatus.DISPATCHED:
            task.dispatched_at = now
        elif status == TaskStatus.ACKNOWLEDGED:
            task.acknowledged_at = now
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            task.completed_at = now

        # Create event
        event_metadata: dict[str, Any] = {"executor_node": task.executor_node}
        if result is not None:
            event_metadata["result"] = result
        if error is not None:
            event_metadata["error"] = error
        event = TaskEvent(
            task_id=task_id,
            event_type=status.value,
            content=self._event_content(task, status, progress_message),
            progress=progress,
            metadata=event_metadata,
        )
        task.events.append(event)

        self._db_update(task)
        return event

    def cleanup_old(self, max_age_seconds: float = 3600.0) -> int:
        """Remove completed tasks older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        to_remove = [
            tid
            for tid, t in self._tasks.items()
            if t.status.is_terminal and (t.completed_at or t.created_at) < cutoff
        ]
        for tid in to_remove:
            task = self._tasks.pop(tid, None)
            if task:
                session_tasks = self._by_session.get(task.session_id, [])
                if tid in session_tasks:
                    session_tasks.remove(tid)
        self._db_delete(to_remove)
        return len(to_remove)

    @staticmethod
    def _event_content(task: Task, status: TaskStatus, msg: str | None) -> str:
        node = task.executor_node or "unknown"
        if status == TaskStatus.DISPATCHED:
            return f"任务已派发到 {node}"
        elif status == TaskStatus.ACKNOWLEDGED:
            return f"{node} 已确认收到任务"
        elif status == TaskStatus.EXECUTING:
            return msg or f"{node} 正在执行..."
        elif status == TaskStatus.COMPLETED:
            return task.result or "任务完成"
        elif status == TaskStatus.FAILED:
            return f"任务失败: {task.error or 'unknown error'}"
        elif status == TaskStatus.TIMED_OUT:
            return f"任务超时: {node} 未响应"
        elif status == TaskStatus.STALE:
            return f"任务停滞: {node} 心跳超时"
        return msg or status.value
