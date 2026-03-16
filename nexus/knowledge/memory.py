"""Layer 3b: explicit episodic memory, separate from session state."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


@dataclass
class EpisodicMemoryEntry:
    entry_id: str
    timestamp: str
    kind: str
    summary: str
    detail: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    importance: int = 1


class EpisodicMemory:
    """
    Long-lived memory store for preferences, decisions, and project facts.

    Design constraints from the migration plan:
    1. This is not conversation cache
    2. Session-bound traces may be recorded, but long-term retention is explicit
    3. Recall should work across summary/detail/metadata/tags
    """

    def __init__(self, storage_path: Path, max_entries: int = 5000):
        self._storage_path = Path(storage_path)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_entries = max_entries
        # 内存缓存 + mtime 追踪，避免每次操作都全量读文件
        self._cache: list[EpisodicMemoryEntry] | None = None
        self._cache_mtime: float = 0.0

    @property
    def storage_path(self) -> Path:
        return self._storage_path

    @property
    def max_entries(self) -> int:
        return self._max_entries

    async def record(
        self,
        *,
        kind: str,
        summary: str,
        detail: str | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        importance: int = 1,
    ) -> EpisodicMemoryEntry:
        entry = EpisodicMemoryEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=datetime.utcnow().isoformat(),
            kind=kind,
            summary=summary.strip(),
            detail=detail.strip() if detail else None,
            tags=sorted({tag.strip() for tag in (tags or []) if str(tag).strip()}),
            metadata=metadata or {},
            session_id=session_id,
            importance=max(1, min(int(importance or 1), 5)),
        )
        entries = self._load_entries()
        entries.append(entry)
        if len(entries) > self._max_entries:
            # 需要截断时才全量重写
            entries = entries[-self._max_entries:]
            self._save_entries(entries)
        else:
            # 正常情况：append-only，只追加一行
            self._append_entry(entry)
        # 更新缓存
        self._cache = entries
        if self._storage_path.exists():
            self._cache_mtime = self._storage_path.stat().st_mtime
        return entry

    async def remember_preference(
        self,
        *,
        summary: str,
        detail: str | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> EpisodicMemoryEntry:
        return await self.record(
            kind="preference",
            summary=summary,
            detail=detail,
            tags=tags,
            metadata=metadata,
            session_id=session_id,
            importance=4,
        )

    async def remember_decision(
        self,
        *,
        summary: str,
        detail: str | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> EpisodicMemoryEntry:
        return await self.record(
            kind="decision",
            summary=summary,
            detail=detail,
            tags=tags,
            metadata=metadata,
            session_id=session_id,
            importance=5,
        )

    async def remember_project_state(
        self,
        *,
        summary: str,
        detail: str | None = None,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> EpisodicMemoryEntry:
        return await self.record(
            kind="project_state",
            summary=summary,
            detail=detail,
            tags=tags,
            metadata=metadata,
            session_id=session_id,
            importance=3,
        )

    async def recall(
        self,
        query: str,
        limit: int = 5,
        *,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        min_importance: int = 1,
    ) -> list[str]:
        entries = await self.recall_entries(
            query,
            limit=limit,
            kinds=kinds,
            tags=tags,
            session_id=session_id,
            min_importance=min_importance,
        )
        return [self._render_entry(entry) for entry in entries]

    async def recall_entries(
        self,
        query: str,
        limit: int = 5,
        *,
        kinds: list[str] | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        min_importance: int = 1,
    ) -> list[EpisodicMemoryEntry]:
        query_lower = query.lower().strip()
        query_tokens = [token for token in query_lower.split() if token]
        required_tags = {tag.lower() for tag in tags or []}
        scored: list[tuple[float, EpisodicMemoryEntry]] = []
        for entry in reversed(self._load_entries()):
            if kinds and entry.kind not in kinds:
                continue
            if session_id and entry.session_id != session_id:
                continue
            if entry.importance < min_importance:
                continue
            tag_set = {tag.lower() for tag in entry.tags}
            if required_tags and not required_tags.issubset(tag_set):
                continue
            score = self._score_entry(entry, query_lower, query_tokens)
            if query_lower and score <= 0:
                continue
            scored.append((score, entry))
        scored.sort(key=lambda item: (item[0], item[1].importance, item[1].timestamp), reverse=True)
        return [entry for _, entry in scored[:limit]]

    def list_recent(
        self,
        limit: int = 20,
        *,
        kind: str | None = None,
        session_id: str | None = None,
    ) -> list[EpisodicMemoryEntry]:
        entries = self._load_entries()
        if kind:
            entries = [entry for entry in entries if entry.kind == kind]
        if session_id:
            entries = [entry for entry in entries if entry.session_id == session_id]
        return entries[-limit:]

    def list_entries_by_session(
        self,
        session_id: str,
        *,
        kind: str | None = None,
    ) -> list[EpisodicMemoryEntry]:
        entries = [entry for entry in self._load_entries() if entry.session_id == session_id]
        if kind:
            entries = [entry for entry in entries if entry.kind == kind]
        return entries

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        grouped: dict[str, list[EpisodicMemoryEntry]] = {}
        for entry in self._load_entries():
            if not entry.session_id:
                continue
            grouped.setdefault(entry.session_id, []).append(entry)
        sessions: list[dict[str, Any]] = []
        for session_id, entries in grouped.items():
            entries.sort(key=lambda item: item.timestamp)
            sessions.append(
                {
                    "session_id": session_id,
                    "title": entries[0].summary,
                    "first_summary": entries[0].summary,
                    "last_summary": entries[-1].summary,
                    "message_count": len(entries),
                    "last_timestamp": entries[-1].timestamp,
                    "entries": entries,
                }
            )
        sessions.sort(key=lambda item: item["last_timestamp"], reverse=True)
        return sessions[:limit]

    def describe(self) -> dict[str, Any]:
        entries = self._load_entries()
        kinds = sorted({entry.kind for entry in entries})
        session_ids = {entry.session_id for entry in entries if entry.session_id}
        return {
            "storage_path": str(self._storage_path),
            "entry_count": len(entries),
            "max_entries": self._max_entries,
            "session_count": len(session_ids),
            "kinds": kinds,
            "compression": {
                "enabled": False,
                "mode": "append_only",
                "note": "长期记忆当前不做语义压缩，只在超过 max_entries 时做尾部截断。",
            },
        }

    def _load_entries(self) -> list[EpisodicMemoryEntry]:
        if not self._storage_path.exists():
            self._cache = []
            self._cache_mtime = 0.0
            return []

        current_mtime = self._storage_path.stat().st_mtime
        if self._cache is not None and current_mtime == self._cache_mtime:
            return list(self._cache)

        entries: list[EpisodicMemoryEntry] = []
        with self._storage_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                entries.append(
                    EpisodicMemoryEntry(
                        entry_id=data.get("entry_id") or data.get("id") or str(uuid.uuid4()),
                        timestamp=data.get("timestamp") or datetime.utcnow().isoformat(),
                        kind=data.get("kind") or "note",
                        summary=data.get("summary") or "",
                        detail=data.get("detail"),
                        tags=list(data.get("tags") or []),
                        metadata=dict(data.get("metadata") or {}),
                        session_id=data.get("session_id"),
                        importance=max(1, min(int(data.get("importance") or 1), 5)),
                    )
                )
        self._cache = entries
        self._cache_mtime = current_mtime
        return list(entries)

    def _append_entry(self, entry: EpisodicMemoryEntry) -> None:
        """Append-only: 追加单条记录到文件末尾，不重写整个文件。"""
        line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
        with self._storage_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _save_entries(self, entries: list[EpisodicMemoryEntry]) -> None:
        """全量重写（仅在 truncate 时使用）。"""
        payload = "\n".join(json.dumps(asdict(entry), ensure_ascii=False) for entry in entries)
        if payload:
            payload += "\n"
        temp = self._storage_path.with_suffix(self._storage_path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(self._storage_path)

    @staticmethod
    def _render_entry(entry: EpisodicMemoryEntry) -> str:
        if entry.detail:
            return f"{entry.summary} | {entry.detail}"
        return entry.summary

    @staticmethod
    def _score_entry(entry: EpisodicMemoryEntry, query: str, query_tokens: list[str]) -> float:
        if not query:
            return float(entry.importance)
        haystack = " ".join(
            filter(
                None,
                [
                    entry.summary,
                    entry.detail or "",
                    " ".join(entry.tags),
                    json.dumps(entry.metadata, ensure_ascii=False),
                ],
            )
        ).lower()
        if not haystack:
            return 0.0
        if query in haystack:
            return 10.0 + entry.importance
        overlap = sum(1 for token in query_tokens if token in haystack)
        return float(overlap + (entry.importance * 0.2))
