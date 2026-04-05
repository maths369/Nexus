"""Persistent registry for subagent executions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class SubagentRecord:
    run_id: str
    prompt_preview: str
    description: str = ""
    spawn_mode: str = "run"
    status: str = "spawned"
    model: str = ""
    depth: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    session_id: str | None = None
    parent_session_id: str | None = None
    parent_run_id: str | None = None
    result: str | None = None
    error: str | None = None
    orphaned: bool = False
    attempts: int = 0
    max_retries: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "prompt_preview": self.prompt_preview,
            "description": self.description,
            "spawn_mode": self.spawn_mode,
            "status": self.status,
            "model": self.model,
            "depth": self.depth,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "parent_run_id": self.parent_run_id,
            "result": self.result,
            "error": self.error,
            "orphaned": self.orphaned,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SubagentRecord":
        return cls(
            run_id=str(payload.get("run_id") or ""),
            prompt_preview=str(payload.get("prompt_preview") or ""),
            description=str(payload.get("description") or ""),
            spawn_mode=str(payload.get("spawn_mode") or "run"),
            status=str(payload.get("status") or "spawned"),
            model=str(payload.get("model") or ""),
            depth=int(payload.get("depth") or 0),
            created_at=str(payload.get("created_at") or datetime.now().isoformat()),
            updated_at=str(payload.get("updated_at") or datetime.now().isoformat()),
            session_id=str(payload.get("session_id") or "") or None,
            parent_session_id=str(payload.get("parent_session_id") or "") or None,
            parent_run_id=str(payload.get("parent_run_id") or "") or None,
            result=str(payload.get("result") or "") or None,
            error=str(payload.get("error") or "") or None,
            orphaned=bool(payload.get("orphaned", False)),
            attempts=int(payload.get("attempts") or 0),
            max_retries=int(payload.get("max_retries") or 3),
        )


class SubagentRegistry:
    """Stores subagent runs under vault/_system/subagent_runs."""

    def __init__(self, base_dir: Path, *, max_retries: int = 3):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._max_retries = max(1, int(max_retries or 3))
        self._notifications: list[dict[str, Any]] = []

    def register_spawn(
        self,
        *,
        run_id: str,
        prompt: str,
        description: str = "",
        spawn_mode: str = "run",
        model: str = "",
        depth: int = 0,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> SubagentRecord:
        record = SubagentRecord(
            run_id=run_id,
            prompt_preview=prompt[:240],
            description=description,
            spawn_mode=spawn_mode,
            model=model,
            depth=depth,
            session_id=session_id,
            parent_session_id=parent_session_id,
            parent_run_id=parent_run_id,
            max_retries=self._max_retries,
        )
        self._write(record)
        return record

    def mark_running(
        self,
        run_id: str,
        *,
        attempts: int | None = None,
        session_id: str | None = None,
    ) -> SubagentRecord:
        record = self._read(run_id)
        record.status = "running"
        record.updated_at = datetime.now().isoformat()
        if attempts is not None:
            record.attempts = int(attempts)
        if session_id:
            record.session_id = session_id
        self._write(record)
        return record

    def mark_completed(
        self,
        run_id: str,
        *,
        result: str,
        session_id: str | None = None,
    ) -> SubagentRecord:
        record = self._read(run_id)
        record.status = "completed"
        record.result = result
        record.error = None
        record.updated_at = datetime.now().isoformat()
        if session_id:
            record.session_id = session_id
        self._write(record)
        self._enqueue_notification(record)
        return record

    def mark_failed(
        self,
        run_id: str,
        *,
        error: str,
        session_id: str | None = None,
        orphaned: bool = False,
    ) -> SubagentRecord:
        record = self._read(run_id)
        record.status = "failed"
        record.error = error
        record.updated_at = datetime.now().isoformat()
        record.orphaned = orphaned
        if session_id:
            record.session_id = session_id
        self._write(record)
        self._enqueue_notification(record)
        return record

    def get(self, run_id: str) -> SubagentRecord:
        return self._read(run_id)

    def recover_orphans(self) -> list[SubagentRecord]:
        recovered: list[SubagentRecord] = []
        for path in sorted(self.base_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = SubagentRecord.from_dict(payload)
            if record.status in {"spawned", "running"}:
                record.status = "failed"
                record.orphaned = True
                record.error = "Subagent run recovered as orphan after process restart."
                record.updated_at = datetime.now().isoformat()
                self._write(record)
                recovered.append(record)
        return recovered

    def drain_notifications(self) -> list[dict[str, Any]]:
        notifications = list(self._notifications)
        self._notifications.clear()
        return notifications

    def _enqueue_notification(self, record: SubagentRecord) -> None:
        if not record.parent_session_id:
            return
        self._notifications.append(
            {
                "run_id": record.run_id,
                "parent_session_id": record.parent_session_id,
                "session_id": record.session_id,
                "status": record.status,
                "result": record.result,
                "error": record.error,
            }
        )

    def _read(self, run_id: str) -> SubagentRecord:
        path = self._record_path(run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SubagentRecord.from_dict(payload)

    def _write(self, record: SubagentRecord) -> None:
        self._record_path(record.run_id).write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _record_path(self, run_id: str) -> Path:
        safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in run_id.strip())
        return self.base_dir / f"{safe_id}.json"
