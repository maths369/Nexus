"""
Audit Log — 变更审计日志 (SQLite)

记录所有进化操作:
- 谁（actor）
- 为什么（action）
- 改了什么（target + details）
- 结果如何（success / error）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import AuditEntry

logger = logging.getLogger(__name__)


class AuditLog:
    """
    SQLite-backed 审计日志。

    所有 Evolution 操作（skill 安装/卸载、config 变更/回滚）
    都必须经过此组件记录。
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    entry_id   TEXT PRIMARY KEY,
                    action     TEXT NOT NULL,
                    timestamp  TEXT NOT NULL,
                    actor      TEXT DEFAULT 'system',
                    target     TEXT DEFAULT '',
                    details    TEXT DEFAULT '{}',
                    success    INTEGER DEFAULT 1,
                    error      TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_action
                    ON audit_log(action, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_audit_target
                    ON audit_log(target, timestamp DESC);
            """)

    def record(
        self,
        action: str,
        target: str = "",
        actor: str = "system",
        details: dict[str, Any] | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> AuditEntry:
        """记录一条审计日志"""
        entry = AuditEntry(
            entry_id=str(uuid.uuid4()),
            action=action,
            timestamp=datetime.now(),
            actor=actor,
            target=target,
            details=details or {},
            success=success,
            error=error,
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (entry_id, action, timestamp, actor, target,
                    details, success, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.entry_id,
                    entry.action,
                    entry.timestamp.isoformat(),
                    entry.actor,
                    entry.target,
                    json.dumps(entry.details, ensure_ascii=False),
                    1 if entry.success else 0,
                    entry.error,
                ),
            )

        log_msg = f"Audit: {action} → {target}"
        if not success:
            log_msg += f" [FAILED: {error or 'unknown'}]"
        logger.info(log_msg)

        return entry

    def query(
        self,
        action: str | None = None,
        target: str | None = None,
        limit: int = 50,
    ) -> list[AuditEntry]:
        """查询审计日志"""
        conditions = []
        params: list[Any] = []

        if action:
            conditions.append("action = ?")
            params.append(action)
        if target:
            conditions.append("target = ?")
            params.append(target)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""SELECT * FROM audit_log {where}
                    ORDER BY timestamp DESC LIMIT ?"""
        params.append(limit)

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        return [
            AuditEntry(
                entry_id=row["entry_id"],
                action=row["action"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                actor=row["actor"],
                target=row["target"],
                details=json.loads(row["details"]) if row["details"] else {},
                success=bool(row["success"]),
                error=row["error"],
            )
            for row in rows
        ]

    def get_recent(self, limit: int = 20) -> list[AuditEntry]:
        """获取最近的审计记录"""
        return self.query(limit=limit)
