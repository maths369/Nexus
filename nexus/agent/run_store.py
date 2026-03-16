"""
Run Store — Run 持久化 (SQLite)

表结构:
  - runs: Run 元数据与状态
  - run_events: Run 执行过程中的事件流
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .types import Run, RunEvent, RunStatus

logger = logging.getLogger(__name__)


class RunStore:
    """
    SQLite-backed Run 持久化。

    设计原则:
    1. Run 是 Agent Core 的概念
    2. 一个 Session 可包含多个 Run
    3. 每个 Run 有完整的事件流用于审计和调试
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id        TEXT PRIMARY KEY,
                    session_id    TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'queued',
                    task          TEXT DEFAULT '',
                    plan          TEXT DEFAULT '',
                    result        TEXT DEFAULT '',
                    error         TEXT,
                    model         TEXT DEFAULT '',
                    attempt_count INTEGER DEFAULT 0,
                    max_attempts  INTEGER DEFAULT 3,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    metadata      TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_runs_session
                    ON runs(session_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_runs_status
                    ON runs(status);

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id    TEXT PRIMARY KEY,
                    run_id      TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    data        TEXT DEFAULT '{}',
                    timestamp   TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_run_events
                    ON run_events(run_id, timestamp);
            """)

    # ------------------------------------------------------------------
    # Run CRUD
    # ------------------------------------------------------------------

    async def save_run(self, run: Run) -> None:
        """保存或更新 Run"""
        run.updated_at = datetime.now()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, session_id, status, task, plan, result, error,
                    model, attempt_count, max_attempts, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    run.session_id,
                    run.status.value,
                    run.task,
                    run.plan,
                    run.result,
                    run.error,
                    run.model,
                    run.attempt_count,
                    run.max_attempts,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                    json.dumps(run.metadata),
                ),
            )

    async def get_run(self, run_id: str) -> Run | None:
        """获取 Run"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row else None

    async def get_runs_by_session(
        self, session_id: str, limit: int = 10
    ) -> list[Run]:
        """获取 session 下的所有 Run"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM runs
                   WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    async def get_active_runs(self) -> list[Run]:
        """获取所有非终态 Run"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM runs
                   WHERE status NOT IN ('succeeded', 'failed')
                   ORDER BY updated_at DESC""",
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Event 操作
    # ------------------------------------------------------------------

    async def save_event(self, event: RunEvent) -> None:
        """保存 Run 事件"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO run_events
                   (event_id, run_id, event_type, data, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.run_id,
                    event.event_type,
                    json.dumps(event.data),
                    event.timestamp.isoformat(),
                ),
            )

    async def get_events(self, run_id: str) -> list[RunEvent]:
        """获取 Run 的所有事件"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM run_events
                   WHERE run_id = ?
                   ORDER BY timestamp""",
                (run_id,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"],
            session_id=row["session_id"],
            status=RunStatus(row["status"]),
            task=row["task"],
            plan=row["plan"],
            result=row["result"],
            error=row["error"],
            model=row["model"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            data=json.loads(row["data"]) if row["data"] else {},
            timestamp=datetime.fromisoformat(row["timestamp"]),
        )
