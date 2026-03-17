"""
Session Store — Session 持久化 (SQLite)

表结构:
  - sessions: session 元数据
  - session_events: session 内的消息与事件流
"""

from __future__ import annotations

import enum
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


@dataclass
class Session:
    session_id: str
    sender_id: str
    channel: str
    status: SessionStatus = SessionStatus.ACTIVE
    summary: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionEvent:
    event_id: str
    session_id: str
    role: str        # "user" | "assistant" | "system" | "tool"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    """
    SQLite-backed session 持久化。

    设计原则:
    1. Session 是 Channel 层的概念，不是 Agent 层的
    2. 一个 session 可以包含多个 run
    3. Session 的生命周期由 Channel 层管理
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表"""
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id   TEXT PRIMARY KEY,
                    sender_id    TEXT NOT NULL,
                    channel      TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'active',
                    summary      TEXT DEFAULT '',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    metadata     TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_sender
                    ON sessions(sender_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_sessions_status
                    ON sessions(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_sessions_sender_channel
                    ON sessions(sender_id, channel, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS session_events (
                    event_id     TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL,
                    role         TEXT NOT NULL,
                    content      TEXT NOT NULL,
                    timestamp    TEXT NOT NULL,
                    metadata     TEXT DEFAULT '{}',
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_session
                    ON session_events(session_id, timestamp);
            """)

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def create_session(
        self, sender_id: str, channel: str, summary: str = ""
    ) -> Session:
        """创建新 session"""
        session = Session(
            session_id=str(uuid.uuid4()),
            sender_id=sender_id,
            channel=channel,
            summary=summary,
        )
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, sender_id, channel, status, summary,
                    created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.sender_id,
                    session.channel,
                    session.status.value,
                    session.summary,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    "{}",
                ),
            )
        logger.info(f"Created session {session.session_id} for {sender_id}")
        return session

    def get_or_create_persistent_session(
        self, sender_id: str, channel: str,
    ) -> tuple[Session, bool]:
        """Get the persistent session for (sender_id, channel), or create one.

        Returns ``(session, created)`` where *created* is ``True`` when a brand
        new session was made.

        Lookup priority:
        1. Active session for this sender + channel
        2. Most recent non-abandoned session for this sender + channel
        3. Create new
        """
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE sender_id = ? AND channel = ?
                         AND status IN ('active', 'paused', 'completed')
                   ORDER BY updated_at DESC LIMIT 1""",
                (sender_id, channel),
            ).fetchone()
        if row:
            session = self._row_to_session(row)
            if session.status != SessionStatus.ACTIVE:
                self.update_session_status(session.session_id, SessionStatus.ACTIVE)
                session.status = SessionStatus.ACTIVE
            self.touch_session(session.session_id)
            return session, False
        session = self.create_session(sender_id=sender_id, channel=channel)
        return session, True

    def get_active_session(self, sender_id: str) -> Session | None:
        """获取用户当前活跃的 session"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE sender_id = ? AND status = 'active'
                   ORDER BY updated_at DESC LIMIT 1""",
                (sender_id,),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def get_session(self, session_id: str) -> Session | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def get_most_recent_session(self, sender_id: str) -> Session | None:
        """获取用户最近的 session（无论状态）"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM sessions
                   WHERE sender_id = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (sender_id,),
            ).fetchone()
        return self._row_to_session(row) if row else None

    def get_recent_sessions(
        self, sender_id: str, limit: int = 5
    ) -> list[Session]:
        """获取用户最近的 N 个 session"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE sender_id = ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (sender_id, limit),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def list_active_sessions(
        self,
        sender_id: str,
        *,
        limit: int = 20,
    ) -> list[Session]:
        """列出用户当前所有活跃 session，按最近更新时间倒序。"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE sender_id = ? AND status = 'active'
                   ORDER BY updated_at DESC LIMIT ?""",
                (sender_id, limit),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def update_session_status(
        self, session_id: str, status: SessionStatus
    ) -> None:
        """更新 session 状态"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """UPDATE sessions
                   SET status = ?, updated_at = ?
                   WHERE session_id = ?""",
                (status.value, datetime.now().isoformat(), session_id),
            )

    def close_other_active_sessions(
        self,
        *,
        sender_id: str,
        keep_session_id: str | None = None,
        new_status: SessionStatus = SessionStatus.ABANDONED,
    ) -> int:
        """将同一 sender 的其他活跃 session 收口，避免 active session 无限累积。"""
        with sqlite3.connect(self._db_path) as conn:
            if keep_session_id:
                result = conn.execute(
                    """UPDATE sessions
                       SET status = ?, updated_at = ?
                       WHERE sender_id = ? AND status = 'active' AND session_id != ?""",
                    (
                        new_status.value,
                        datetime.now().isoformat(),
                        sender_id,
                        keep_session_id,
                    ),
                )
            else:
                result = conn.execute(
                    """UPDATE sessions
                       SET status = ?, updated_at = ?
                       WHERE sender_id = ? AND status = 'active'""",
                    (
                        new_status.value,
                        datetime.now().isoformat(),
                        sender_id,
                    ),
                )
        return int(result.rowcount or 0)

    def touch_session(self, session_id: str) -> None:
        """更新 session 的 updated_at 时间戳"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """UPDATE sessions SET updated_at = ? WHERE session_id = ?""",
                (datetime.now().isoformat(), session_id),
            )

    def update_session_summary(self, session_id: str, summary: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """UPDATE sessions
                   SET summary = ?, updated_at = ?
                   WHERE session_id = ?""",
                (summary.strip(), datetime.now().isoformat(), session_id),
            )

    def update_session_metadata(self, session_id: str, updates: dict[str, Any]) -> None:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        metadata = dict(session.metadata)
        metadata.update(updates)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """UPDATE sessions
                   SET metadata = ?, updated_at = ?
                   WHERE session_id = ?""",
                (json.dumps(metadata, ensure_ascii=False), datetime.now().isoformat(), session_id),
            )

    def append_recent_artifacts(
        self,
        session_id: str,
        artifacts: list[dict[str, Any]],
        *,
        limit: int = 5,
    ) -> None:
        """将最近导入的附件摘要写入 session metadata，供后续会话引用。"""
        if not artifacts:
            return
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")

        existing = [
            item
            for item in session.metadata.get("recent_artifacts", [])
            if isinstance(item, dict)
        ]
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in [*artifacts, *existing]:
            artifact_id = str(item.get("artifact_id") or "")
            if artifact_id and artifact_id in seen:
                continue
            if artifact_id:
                seen.add(artifact_id)
            normalized.append(
                {
                    "artifact_id": artifact_id,
                    "artifact_type": str(item.get("artifact_type") or "file"),
                    "filename": str(item.get("filename") or ""),
                    "relative_path": str(item.get("relative_path") or ""),
                    "page_relative_path": str(item.get("page_relative_path") or ""),
                    "transcript_relative_path": str(item.get("transcript_relative_path") or ""),
                    "status": str(item.get("status") or ""),
                }
            )
        self.update_session_metadata(
            session_id,
            {"recent_artifacts": normalized[:limit]},
        )

    def get_recent_artifacts(
        self,
        session_id: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        session = self.get_session(session_id)
        if session is None:
            return []
        items = session.metadata.get("recent_artifacts", [])
        if not isinstance(items, list):
            return []
        artifacts = [item for item in items if isinstance(item, dict)]
        return artifacts[:limit]

    # ------------------------------------------------------------------
    # Event 操作
    # ------------------------------------------------------------------

    def add_event(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionEvent:
        """向 session 添加事件"""
        event = SessionEvent(
            event_id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata or {},
        )
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO session_events
                   (event_id, session_id, role, content, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.session_id,
                    event.role,
                    event.content,
                    event.timestamp.isoformat(),
                    json.dumps(event.metadata, ensure_ascii=False),
                ),
            )
        self.touch_session(session_id)
        return event

    def get_events(
        self, session_id: str, limit: int | None = None
    ) -> list[SessionEvent]:
        """获取 session 的事件列表"""
        query = """SELECT * FROM session_events
                   WHERE session_id = ?
                   ORDER BY timestamp"""
        params: tuple = (session_id,)
        if limit:
            query += " DESC LIMIT ?"
            params = (session_id, limit)
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def find_relevant_sessions(
        self,
        *,
        sender_id: str,
        query: str,
        limit: int = 5,
    ) -> list[Session]:
        query_text = query.strip().lower()
        if not query_text:
            return self.get_recent_sessions(sender_id=sender_id, limit=limit)
        query_tokens = {token for token in query_text.split() if token}
        candidates: list[tuple[float, Session]] = []
        for session in self.get_recent_sessions(sender_id=sender_id, limit=20):
            events = self.get_events(session.session_id, limit=4)
            haystack_parts = [session.summary]
            haystack_parts.extend(event.content for event in events)
            haystack = " ".join(part for part in haystack_parts if part).lower()
            if not haystack:
                continue
            score = 0.0
            if query_text in haystack:
                score += 10.0
            score += sum(1.0 for token in query_tokens if token in haystack)
            if session.status == SessionStatus.ACTIVE:
                score += 1.5
            if score <= 0:
                continue
            candidates.append((score, session))
        candidates.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [session for _, session in candidates[:limit]]

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"],
            sender_id=row["sender_id"],
            channel=row["channel"],
            status=SessionStatus(row["status"]),
            summary=row["summary"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> SessionEvent:
        return SessionEvent(
            event_id=row["event_id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )
