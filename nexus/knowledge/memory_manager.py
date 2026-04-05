"""
MemoryManager — 长期记忆管理中枢

复制 OpenClaw 的完整记忆能力:
1. SOUL.md   — Agent 身份/人格持久化
2. USER.md   — 用户画像积累
3. Daily Journal — 每日记忆日志 (memory/YYYY-MM-DD.md)
4. Semantic Memory Search — 基于 RetrievalIndex 的语义搜索 + 时间衰减
5. Memory Flush — 压缩前自动保存关键记忆
6. Memory Index — 记忆条目索引到 RetrievalIndex 实现语义检索
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .medical_kb import (
    MEDICAL_KB_ROOT,
    build_sync_metadata,
    conflict_relative_path,
    l3_relative_path,
    l4_relative_path,
    normalize_l3_folder,
    normalize_l4_section,
    render_markdown_document,
    weekly_summary_relative_path,
)
from .memory import EpisodicMemory, EpisodicMemoryEntry

if TYPE_CHECKING:
    from .retrieval import RetrievalIndex, RetrievalResult
    from nexus.channel.session_store import Session, SessionEvent, SessionStore
    from nexus.provider.gateway import ProviderGateway
    from nexus.services.document import DocumentService

logger = logging.getLogger(__name__)

# 默认时间衰减半衰期（天）
DEFAULT_HALF_LIFE_DAYS = 30.0

# 记忆源前缀，用于在 RetrievalIndex 中区分记忆和文档
MEMORY_SOURCE_PREFIX = "memory://"
IDENTITY_SOURCE_PREFIX = "identity://"
JOURNAL_SOURCE_PREFIX = "journal://"

_MEMORY_KIND_ALIASES = {
    "pref": "preference",
    "preference": "preference",
    "decision": "decision",
    "fact": "fact",
    "project": "project_state",
    "project_state": "project_state",
    "context": "context",
    "workflow": "workflow_success",
    "workflow_success": "workflow_success",
    "workflow_failure": "workflow_failure",
    "failure": "workflow_failure",
    "tool_pattern": "tool_pattern",
}

_DEFAULT_MEMORY_KINDS = {
    "preference",
    "decision",
    "fact",
    "project_state",
    "context",
    "workflow_success",
    "workflow_failure",
    "tool_pattern",
}

_PROMPT_KIND_LABELS = {
    "preference": "偏好",
    "decision": "决策",
    "fact": "事实",
    "project_state": "项目状态",
    "context": "上下文",
    "workflow_success": "成功经验",
    "workflow_failure": "失败教训",
    "tool_pattern": "工具模式",
    "identity": "身份",
    "journal": "日志",
}


class MemoryManager:
    """
    记忆管理中枢。

    组合 EpisodicMemory（持久存储）+ RetrievalIndex（语义检索）+
    Vault 文件系统（身份文件 & 日志）。
    """

    def __init__(
        self,
        *,
        memory: EpisodicMemory,
        retrieval: RetrievalIndex,
        vault_path: Path,
        provider: ProviderGateway | None = None,
        session_store: SessionStore | None = None,
        document_service: DocumentService | None = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ):
        self._memory = memory
        self._retrieval = retrieval
        self._vault_path = Path(vault_path)
        self._provider = provider
        self._session_store = session_store
        self._document_service = document_service
        self._half_life_days = half_life_days

        # 身份文件路径
        self._soul_path = self._vault_path / "_system" / "memory" / "SOUL.md"
        self._user_path = self._vault_path / "_system" / "memory" / "USER.md"
        self._journal_dir = self._vault_path / "_system" / "memory" / "journals"

        # 确保目录存在
        self._soul_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        self._last_sync_at: datetime | None = None
        self._last_sync_summary: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 语义记忆搜索（核心突破点）
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        use_time_decay: bool = True,
        min_score: float = 0.01,
        kinds: list[str] | None = None,
        include_identity: bool = True,
        include_journals: bool = True,
    ) -> list[dict[str, Any]]:
        """
        语义记忆搜索 — 基于 RetrievalIndex 的混合检索 + 时间衰减。

        与旧的 EpisodicMemory.recall 的区别:
        - 使用 FTS5 + 可选嵌入向量的混合检索（而非纯关键词匹配）
        - 支持时间衰减（近期记忆权重更高）
        - 返回结构化结果（含来源、分数、元数据）
        """
        allowed_kinds = {
            self._normalize_kind(kind)
            for kind in (kinds or [])
            if str(kind).strip()
        } or None
        allowed_prefixes = [MEMORY_SOURCE_PREFIX]
        if include_identity:
            allowed_prefixes.append(IDENTITY_SOURCE_PREFIX)
        if include_journals:
            allowed_prefixes.append(JOURNAL_SOURCE_PREFIX)

        all_results = await self._retrieval.search(
            query,
            top_k=max(top_k * 8, 24),
            min_score=0.0,
        )

        memory_results = [
            r for r in all_results
            if any(r.source.startswith(prefix) for prefix in allowed_prefixes)
            and (
                allowed_kinds is None
                or self._infer_result_kind(r) in allowed_kinds
            )
        ]

        # 应用时间衰减
        if use_time_decay and memory_results:
            memory_results = self._apply_time_decay(memory_results)

        # 过滤最低分并排序
        memory_results = [r for r in memory_results if r.score >= min_score]
        memory_results.sort(key=lambda r: r.score, reverse=True)
        memory_results = memory_results[:top_k]

        # 同时从 EpisodicMemory 做关键词召回作为补充
        episodic_results = await self._memory.recall_entries(
            query,
            limit=max(top_k * 2, 10),
            kinds=sorted(allowed_kinds) if allowed_kinds else None,
        )
        episodic_ids = {r.source.replace(MEMORY_SOURCE_PREFIX, "") for r in memory_results}

        # 合并去重
        merged: list[dict[str, Any]] = []
        for r in memory_results:
            entry_id = r.source.replace(MEMORY_SOURCE_PREFIX, "")
            kind = self._infer_result_kind(r)
            merged.append({
                "entry_id": entry_id,
                "content": r.content,
                "score": round(r.score, 4),
                "source": "semantic",
                "metadata": {
                    **r.metadata,
                    "kind": kind,
                },
            })

        for entry in episodic_results:
            if entry.entry_id not in episodic_ids:
                merged.append({
                    "entry_id": entry.entry_id,
                    "content": f"{entry.summary}" + (f" | {entry.detail}" if entry.detail else ""),
                    "score": round(entry.importance * 0.1, 4),
                    "source": "keyword",
                    "metadata": {
                        "kind": entry.kind,
                        "tags": entry.tags,
                        "timestamp": entry.timestamp,
                    },
                })

        # 最终排序并截断
        merged.sort(
            key=lambda x: (
                x["score"],
                self._kind_rank(str(x.get("metadata", {}).get("kind") or "")),
            ),
            reverse=True,
        )
        return merged[:top_k]

    async def build_prompt_context(
        self,
        query: str,
        *,
        top_k: int = 6,
        max_chars: int = 1_800,
        include_journals: bool = True,
    ) -> str:
        results = await self.search(
            query,
            top_k=top_k,
            include_identity=False,
            include_journals=include_journals,
        )
        if not results:
            return ""

        lines: list[str] = []
        total_chars = 0
        for item in results:
            line = self._format_prompt_memory_item(item)
            if not line:
                continue
            projected = total_chars + len(line) + 1
            if projected > max_chars and lines:
                break
            lines.append(line)
            total_chars = projected
        return "\n".join(lines)

    async def sync_retrieval_sources_if_due(
        self,
        *,
        min_interval_seconds: float = 60.0,
        delta_only: bool = True,
        include_vault: bool = True,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if self._last_sync_at is not None:
            elapsed = (now - self._last_sync_at).total_seconds()
            if elapsed < min_interval_seconds:
                return {
                    **self._last_sync_summary,
                    "skipped": True,
                    "reason": "throttled",
                    "elapsed_seconds": round(elapsed, 3),
                }

        stats = await self.sync_retrieval_sources(
            delta_only=delta_only,
            include_vault=include_vault,
        )
        self._last_sync_at = now
        self._last_sync_summary = dict(stats)
        return stats

    async def sync_retrieval_sources(
        self,
        *,
        delta_only: bool = True,
        include_vault: bool = True,
    ) -> dict[str, Any]:
        removed = await self._remove_missing_retrieval_sources()
        memory_reindex = {"indexed": 0, "errors": 0, "total": 0}
        if self._needs_memory_reindex():
            memory_reindex = await self.reindex_all_memories()
        identity = await self.reindex_identity_documents(force=not delta_only)
        journals = await self.reindex_journals(force=not delta_only)
        vault = (
            self._retrieval.reindex_vault(self._vault_path, delta_only=delta_only)
            if include_vault
            else {"files_processed": 0, "chunks_created": 0, "errors": 0, "files_skipped": 0}
        )
        return {
            "memory_reindex": memory_reindex,
            "identity": identity,
            "journals": journals,
            "vault": vault,
            "removed_sources": removed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "skipped": False,
        }

    async def reindex_journals(self, *, force: bool = False) -> dict[str, int]:
        stats = {"files_processed": 0, "chunks_created": 0, "errors": 0, "files_skipped": 0}
        for journal_path in sorted(self._journal_dir.glob("*.md")):
            source = f"{JOURNAL_SOURCE_PREFIX}{journal_path.stem}"
            try:
                content = journal_path.read_text(encoding="utf-8")
                content_hash = self._retrieval.compute_content_hash(content)
                if not force and self._retrieval.has_same_hash(source, content_hash):
                    stats["files_skipped"] += 1
                    continue
                chunks = await self._retrieval.index_document(
                    source=source,
                    content=content,
                    metadata={
                        "source": "journal",
                        "date": journal_path.stem,
                        "file_name": journal_path.name,
                        "file_modified": datetime.fromtimestamp(journal_path.stat().st_mtime).isoformat(),
                        "indexed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    force=force,
                )
                stats["files_processed"] += 1
                stats["chunks_created"] += chunks
            except Exception:
                stats["errors"] += 1
                logger.warning("Failed to index journal %s", journal_path.name, exc_info=True)
        return stats

    async def reindex_all_retrieval_sources(self) -> dict[str, Any]:
        return await self.sync_retrieval_sources(delta_only=False, include_vault=True)

    # ------------------------------------------------------------------
    # 记忆保存（写入 Episodic + 索引到 Retrieval）
    # ------------------------------------------------------------------

    async def save(
        self,
        *,
        summary: str,
        detail: str | None = None,
        kind: str = "fact",
        tags: list[str] | None = None,
        importance: int = 3,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        保存记忆条目。

        双写: EpisodicMemory (JSONL 持久) + RetrievalIndex (语义索引)。
        """
        normalized_kind = self._normalize_kind(kind)
        clean_tags = self._normalize_tags(tags)
        # 1. 写入 EpisodicMemory
        entry = await self._memory.record(
            kind=normalized_kind,
            summary=summary.strip(),
            detail=detail.strip() if detail else None,
            tags=clean_tags,
            session_id=session_id,
            importance=importance,
            metadata={
                "kind": normalized_kind,
                "tags": clean_tags,
            },
        )

        # 2. 索引到 RetrievalIndex
        await self._index_memory_entry(entry)

        return {
            "entry_id": entry.entry_id,
            "kind": entry.kind,
            "summary": entry.summary,
            "indexed": True,
        }

    async def _index_memory_entry(self, entry: EpisodicMemoryEntry) -> None:
        """将单条记忆索引到 RetrievalIndex"""
        source = f"{MEMORY_SOURCE_PREFIX}{entry.entry_id}"
        content_parts = [entry.summary]
        if entry.detail:
            content_parts.append(entry.detail)
        if entry.tags:
            content_parts.append(f"Tags: {', '.join(entry.tags)}")
        content = "\n".join(content_parts)

        metadata = {
            "kind": entry.kind,
            "importance": entry.importance,
            "tags": entry.tags,
            "timestamp": entry.timestamp,
            "session_id": entry.session_id,
            "source": "memory",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await self._retrieval.index_document(
                source=source,
                content=content,
                metadata=metadata,
                force=True,
            )
        except Exception as e:
            logger.warning("Failed to index memory entry %s: %s", entry.entry_id, e)

    async def reindex_all_memories(self) -> dict[str, int]:
        """重建所有记忆的语义索引"""
        entries = self._memory._load_entries()
        indexed = 0
        errors = 0
        for entry in entries:
            try:
                await self._index_memory_entry(entry)
                indexed += 1
            except Exception:
                errors += 1
        return {"indexed": indexed, "errors": errors, "total": len(entries)}

    # ------------------------------------------------------------------
    # 身份持久化: SOUL.md
    # ------------------------------------------------------------------

    def read_soul(self) -> str:
        """读取 SOUL.md — Agent 身份与人格"""
        if not self._soul_path.exists():
            return ""
        return self._soul_path.read_text(encoding="utf-8")

    async def update_soul(self, content: str) -> str:
        """更新 SOUL.md"""
        self._soul_path.parent.mkdir(parents=True, exist_ok=True)
        self._soul_path.write_text(content, encoding="utf-8")
        await self.reindex_identity_documents(force=False)
        logger.info("SOUL.md updated, %d chars", len(content))
        return f"SOUL.md 已更新（{len(content)} 字符）"

    # ------------------------------------------------------------------
    # 用户画像: USER.md
    # ------------------------------------------------------------------

    def read_user_profile(self) -> str:
        """读取 USER.md — 用户画像"""
        if not self._user_path.exists():
            return ""
        return self._user_path.read_text(encoding="utf-8")

    async def update_user_profile(self, section: str, content: str) -> str:
        """
        更新 USER.md 中的某个 section。

        如果 section 已存在，替换其内容；否则追加新 section。
        """
        current = self.read_user_profile()

        if not current:
            # 初始化 USER.md
            current = (
                "# USER\n\n"
                "## MACHINE SUMMARY\n\n"
                "```yaml\n{}\n```\n\n"
                "## 用户画像\n\n"
            )

        # 查找并替换 section
        section_header = f"## {section}"
        lines = current.split("\n")
        new_lines: list[str] = []
        in_target_section = False
        section_replaced = False

        for line in lines:
            if line.strip().startswith("## "):
                if in_target_section:
                    in_target_section = False
                if line.strip() == section_header:
                    in_target_section = True
                    section_replaced = True
                    new_lines.append(section_header)
                    new_lines.append("")
                    new_lines.append(content.strip())
                    new_lines.append("")
                    continue
            if not in_target_section:
                new_lines.append(line)

        if not section_replaced:
            # 追加新 section
            new_lines.append("")
            new_lines.append(section_header)
            new_lines.append("")
            new_lines.append(content.strip())
            new_lines.append("")

        updated = "\n".join(new_lines)
        self._user_path.write_text(updated, encoding="utf-8")
        await self.reindex_identity_documents(force=False)
        logger.info("USER.md section '%s' updated", section)
        return f"USER.md 的 [{section}] 已更新"

    # ------------------------------------------------------------------
    # 每日记忆日志
    # ------------------------------------------------------------------

    async def append_daily_journal(
        self,
        content: str,
        *,
        date: str | None = None,
    ) -> str:
        """
        追加内容到当天的记忆日志。

        日志路径: vault/_system/memory/journals/YYYY-MM-DD.md
        """
        today = date or datetime.now().strftime("%Y-%m-%d")
        journal_path = self._journal_dir / f"{today}.md"

        if journal_path.exists():
            existing = journal_path.read_text(encoding="utf-8")
        else:
            existing = f"# 记忆日志 {today}\n\n"

        timestamp = datetime.now().strftime("%H:%M")
        entry = f"### {timestamp}\n\n{content.strip()}\n\n"

        journal_path.write_text(existing + entry, encoding="utf-8")
        logger.info("Daily journal %s appended", today)

        # 索引到 RetrievalIndex
        try:
            await self._retrieval.index_document(
                source=f"{JOURNAL_SOURCE_PREFIX}{today}",
                content=journal_path.read_text(encoding="utf-8"),
                metadata={
                    "source": "journal",
                    "date": today,
                    "indexed_at": datetime.now(timezone.utc).isoformat(),
                },
                force=True,
            )
        except Exception as e:
            logger.warning("Failed to index journal %s: %s", today, e)

        return f"已追加到日志 {today}"

    def read_daily_journal(self, date: str | None = None) -> str:
        """读取指定日期的记忆日志"""
        today = date or datetime.now().strftime("%Y-%m-%d")
        journal_path = self._journal_dir / f"{today}.md"
        if not journal_path.exists():
            return ""
        return journal_path.read_text(encoding="utf-8")

    def list_journals(self, limit: int = 30) -> list[dict[str, Any]]:
        """列出最近的记忆日志"""
        journals = sorted(self._journal_dir.glob("*.md"), reverse=True)[:limit]
        return [
            {
                "date": j.stem,
                "path": str(j),
                "size": j.stat().st_size,
            }
            for j in journals
        ]

    # ------------------------------------------------------------------
    # 压缩前记忆 Flush（核心机制）
    # ------------------------------------------------------------------

    async def flush_before_compact(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        在上下文压缩前，提取并保存关键记忆。

        步骤:
        1. 用 LLM 从当前对话中提取值得长期保存的记忆
        2. 每条保存到 EpisodicMemory + RetrievalIndex
        3. 追加到当天的日志
        """
        if not self._provider:
            # 无 LLM 时用规则提取
            return await self._rule_based_flush(messages)

        # 构建对话文本
        conversation = self._messages_to_text(messages)
        if len(conversation) > 40_000:
            conversation = conversation[:20_000] + "\n...\n" + conversation[-20_000:]

        try:
            response = await self._provider.chat_completion(
                messages=[{
                    "role": "user",
                    "content": (
                        "你是记忆管理系统。请从以下对话中提取值得长期保存的记忆条目。\n\n"
                        "提取规则:\n"
                        "1. 用户的重要决策、偏好、习惯\n"
                        "2. 项目的关键进展、架构决策、技术选型\n"
                        "3. 重要的事实、结论、待办事项\n"
                        "4. 不要提取临时对话、寒暄、已完成的一次性操作\n\n"
                        "输出格式（JSON 数组）:\n"
                        '```json\n[\n  {"summary": "...", "detail": "...", "kind": "decision|preference|fact|project_state", '
                        '"tags": ["tag1", "tag2"], "importance": 1-5}\n]\n```\n\n'
                        "如果没有值得保存的内容，返回空数组 `[]`。\n\n"
                        f"--- 对话内容 ---\n{conversation}"
                    ),
                }],
                max_tokens=2000,
                temperature=0.3,
            )
            content = response.get("message", {}).get("content", "")
            memories = self._parse_memory_extraction(content)
        except Exception as e:
            logger.warning("Memory flush LLM call failed: %s", e)
            memories = []

        saved_count = 0
        journal_parts: list[str] = []

        for mem in memories:
            try:
                result = await self.save(
                    summary=mem.get("summary", ""),
                    detail=mem.get("detail"),
                    kind=mem.get("kind", "fact"),
                    tags=mem.get("tags"),
                    importance=mem.get("importance", 3),
                )
                saved_count += 1
                journal_parts.append(f"- [{mem.get('kind', 'fact')}] {mem.get('summary', '')}")
            except Exception as e:
                logger.warning("Failed to save flushed memory: %s", e)

        # 写入当天日志
        if journal_parts:
            journal_content = "**压缩前记忆保存:**\n\n" + "\n".join(journal_parts)
            await self.append_daily_journal(journal_content)

        return {"saved": saved_count, "total_extracted": len(memories)}

    async def _rule_based_flush(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """无 LLM 时的规则提取"""
        saved = 0
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str) or len(content) < 20:
                continue
            # 保存用户的较长消息作为 context
            await self.save(
                summary=content[:200],
                detail=content[:500] if len(content) > 200 else None,
                kind="context",
                importance=2,
            )
            saved += 1
            if saved >= 5:
                break
        return {"saved": saved, "total_extracted": saved}

    # ------------------------------------------------------------------
    # 获取身份上下文（用于注入 system prompt）
    # ------------------------------------------------------------------

    def get_identity_context(self) -> str:
        """获取 SOUL.md + USER.md 内容，用于注入 system prompt"""
        parts: list[str] = []

        soul = self.read_soul()
        if soul:
            parts.append(f"## Agent 身份\n{soul}")

        user = self.read_user_profile()
        if user:
            parts.append(f"## 用户画像\n{user}")

        return "\n\n".join(parts)

    async def capture_workflow_outcome(
        self,
        *,
        task: str,
        result: str,
        events: list[dict[str, Any]] | list[Any],
        success: bool,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        task_text = task.strip()
        if not task_text:
            return {"saved": 0, "reason": "empty_task"}

        tool_stats = self._extract_tool_statistics(events)
        task_signature = self._task_signature(task_text)
        task_tags = self._build_task_tags(task_text, tool_stats["successful_tools"])
        result_excerpt = self._truncate_text(result.strip(), limit=500)
        outcome_kind = "workflow_success" if success else "workflow_failure"
        summary_prefix = "成功完成" if success else "任务失败"
        detail_lines = [
            f"Task: {task_text}",
            f"Outcome: {'success' if success else 'failure'}",
        ]
        if result_excerpt:
            detail_lines.append(f"Result: {result_excerpt}")
        if tool_stats["successful_tools"]:
            detail_lines.append(
                "Successful tools: " + ", ".join(tool_stats["successful_tools"][:8])
            )
        if tool_stats["failed_tools"]:
            detail_lines.append(
                "Failed tools: " + ", ".join(tool_stats["failed_tools"][:8])
            )
        if tool_stats["artifacts"]:
            detail_lines.append(
                "Artifacts: " + ", ".join(tool_stats["artifacts"][:5])
            )

        saved = await self.save(
            summary=f"{summary_prefix}: {self._truncate_text(task_text, limit=90)}",
            detail="\n".join(detail_lines),
            kind=outcome_kind,
            tags=task_tags,
            importance=4 if success else 3,
            session_id=session_id,
        )

        entry_id = str(saved.get("entry_id") or "")
        if entry_id:
            self._update_memory_entry_metadata(
                entry_id,
                {
                    "kind": outcome_kind,
                    "task": task_text,
                    "task_signature": task_signature,
                    "successful_tools": tool_stats["successful_tools"],
                    "failed_tools": tool_stats["failed_tools"],
                    "artifacts": tool_stats["artifacts"],
                    "run_id": run_id,
                    "success": success,
                    "result_excerpt": result_excerpt,
                },
            )
            await self._reindex_memory_entry_by_id(entry_id)

        return {
            "saved": 1 if entry_id else 0,
            "entry_id": entry_id,
            "task_signature": task_signature,
            "successful_tools": tool_stats["successful_tools"],
            "failed_tools": tool_stats["failed_tools"],
        }

    async def promote_session_to_medical_knowledge(self, *, session_id: str) -> dict[str, Any]:
        if self._session_store is None or self._document_service is None:
            return {"promoted": False, "reason": "not_configured"}
        if self._provider is None:
            return {"promoted": False, "reason": "no_provider"}

        session = self._session_store.get_session(session_id)
        if session is None:
            return {"promoted": False, "reason": "unknown_session"}
        if not str(session.channel or "").startswith("feishu"):
            return {"promoted": False, "reason": "non_feishu"}

        events = self._session_store.get_events(session_id)
        if not events:
            return {"promoted": False, "reason": "no_events"}

        state = session.metadata.get("medical_kb_promotion", {})
        if not isinstance(state, dict):
            state = {}
        last_event_count = self._coerce_int(state.get("last_event_count"), default=0)
        if len(events) <= last_event_count:
            return {"promoted": False, "reason": "no_new_events"}

        pending_events = events[last_event_count:]
        conversation = self._session_events_to_text(session, pending_events)
        if not conversation.strip():
            self._record_medical_kb_promotion(
                session_id,
                last_event_count=len(events),
                promoted=False,
                reason="empty_delta",
            )
            return {"promoted": False, "reason": "empty_delta"}

        response = await self._provider.chat_completion(
            messages=[{
                "role": "user",
                "content": (
                    "你是 Nexus 的医疗器械工程知识提升器。"
                    "请把下面这段飞书会话增量判断是否应沉淀到知识库与记忆系统。\n\n"
                    "输出必须是 JSON 对象，字段固定为：\n"
                    "{\n"
                    '  "medical_relevant": true|false,\n'
                    '  "l2_memories": [{"summary":"", "detail":"", "kind":"decision|preference|fact|project_state|context", "tags":[""], "importance":1-5}],\n'
                    '  "l3_entries": [{"folder":"adr|meeting|question|weekly", "title":"", "summary":"", "body_markdown":"", "tags":[""], "promotion_state":"working"}],\n'
                    '  "l4_entries": [{"section":"index|regulation|device|discipline|tools|learning", "title":"", "summary":"", "body_markdown":"", "tags":[""], "promotion_state":"published"}],\n'
                    '  "weekly_summary": {"title":"", "body_markdown":"", "week_focus":[""]}\n'
                    "}\n\n"
                    "规则：\n"
                    "1. 只有医疗器械工程相关内容才设为 medical_relevant=true。\n"
                    "2. L2 只保留高价值决策、偏好、项目状态、重要事实。\n"
                    "3. L3 放中间层工作文档，不确定的问题留在 question。\n"
                    "4. L4 只放已经稳定、可复用的正式知识。\n"
                    "5. body_markdown 必须是可直接落盘的 Markdown 正文，不要包裹 JSON。\n"
                    "6. 如果没有可提升内容，也要返回空数组和空对象。\n\n"
                    f"session_id: {session.session_id}\n"
                    f"channel: {session.channel}\n"
                    f"summary: {session.summary}\n\n"
                    f"--- 会话增量 ---\n{conversation}"
                ),
            }],
            max_tokens=2800,
            temperature=0.2,
        )

        payload = self._parse_medical_promotion_payload(
            response.get("message", {}).get("content", "")
        )
        if not payload.get("medical_relevant"):
            self._record_medical_kb_promotion(
                session_id,
                last_event_count=len(events),
                promoted=False,
                reason="not_medical_relevant",
            )
            return {"promoted": False, "reason": "not_medical_relevant"}

        l2_saved = 0
        l3_written = 0
        l4_written = 0
        conflicts = 0
        unchanged = 0
        weekly_updated = False

        for memory in payload.get("l2_memories", []):
            summary = str(memory.get("summary") or "").strip()
            if not summary:
                continue
            await self.save(
                summary=summary,
                detail=str(memory.get("detail") or "").strip() or None,
                kind=str(memory.get("kind") or "fact"),
                tags=self._normalize_tags(memory.get("tags")),
                importance=max(1, min(self._coerce_int(memory.get("importance"), default=3), 5)),
                session_id=session_id,
            )
            l2_saved += 1

        for entry in payload.get("l3_entries", []):
            folder = normalize_l3_folder(entry.get("folder"))
            title = str(entry.get("title") or entry.get("summary") or "").strip()
            if folder is None or not title:
                continue
            result = await self._write_promoted_kb_entry(
                session=session,
                relative_path=l3_relative_path(folder, title),
                title=title,
                body=self._build_promoted_body(entry),
                kb_level="L3",
                promotion_state=str(entry.get("promotion_state") or "working"),
            )
            if result["status"] == "written":
                l3_written += 1
            elif result["status"] == "conflict":
                conflicts += 1
            elif result["status"] == "unchanged":
                unchanged += 1

        for entry in payload.get("l4_entries", []):
            section = normalize_l4_section(entry.get("section"))
            title = str(entry.get("title") or entry.get("summary") or "").strip()
            if section is None or not title:
                continue
            result = await self._write_promoted_kb_entry(
                session=session,
                relative_path=l4_relative_path(section, title),
                title=title,
                body=self._build_promoted_body(entry),
                kb_level="L4",
                promotion_state=str(entry.get("promotion_state") or "published"),
            )
            if result["status"] == "written":
                l4_written += 1
            elif result["status"] == "conflict":
                conflicts += 1
            elif result["status"] == "unchanged":
                unchanged += 1

        weekly_summary = payload.get("weekly_summary")
        if isinstance(weekly_summary, dict):
            weekly_result = await self._append_weekly_summary(session, pending_events, weekly_summary)
            weekly_updated = weekly_result.get("status") == "written"
            if weekly_result.get("status") == "unchanged":
                unchanged += 1

        promoted = any([l2_saved, l3_written, l4_written, weekly_updated])
        self._record_medical_kb_promotion(
            session_id,
            last_event_count=len(events),
            promoted=promoted,
            reason="ok" if promoted else "no_materialized_output",
            summary={
                "l2_saved": l2_saved,
                "l3_written": l3_written,
                "l4_written": l4_written,
                "conflicts": conflicts,
                "unchanged": unchanged,
                "weekly_updated": weekly_updated,
            },
        )
        return {
            "promoted": promoted,
            "l2_saved": l2_saved,
            "l3_written": l3_written,
            "l4_written": l4_written,
            "conflicts": conflicts,
            "unchanged": unchanged,
            "weekly_updated": weekly_updated,
        }

    def suggest_evolution_opportunity(
        self,
        *,
        task: str,
        min_occurrences: int = 3,
    ) -> dict[str, Any] | None:
        signature = self._task_signature(task)
        if not signature:
            return None

        matches = [
            entry
            for entry in self._memory._load_entries()
            if entry.kind == "workflow_success"
            and str(entry.metadata.get("task_signature") or "") == signature
        ]
        if len(matches) < min_occurrences:
            return None

        tool_counts = Counter()
        for entry in matches:
            for tool_name in entry.metadata.get("successful_tools", []) or []:
                if str(tool_name).strip():
                    tool_counts[str(tool_name).strip()] += 1

        recommended_tools = [
            tool_name
            for tool_name, _count in tool_counts.most_common(5)
        ]
        example_summaries = [entry.summary for entry in matches[-3:]]

        return {
            "kind": "skill_candidate",
            "reason": "repeated_successful_workflow",
            "task_signature": signature,
            "occurrence_count": len(matches),
            "suggested_skill_id": self._suggest_skill_id(signature, recommended_tools),
            "recommended_tools": recommended_tools,
            "examples": example_summaries,
        }

    async def _write_promoted_kb_entry(
        self,
        *,
        session: Session,
        relative_path: str,
        title: str,
        body: str,
        kb_level: str,
        promotion_state: str,
    ) -> dict[str, Any]:
        assert self._document_service is not None

        metadata = build_sync_metadata(
            relative_path=relative_path,
            kb_level=kb_level,
            source_channel=session.channel,
            source_session_id=session.session_id,
            promotion_state=promotion_state,
        )
        content = render_markdown_document(
            title=title,
            body=body,
            metadata=metadata,
        )
        if self._document_service._content.exists(relative_path):  # noqa: SLF001
            existing = self._document_service.read_page(relative_path)
            if existing.strip() == content.strip():
                return {"status": "unchanged", "relative_path": relative_path}

            incoming_path = self._conflict_variant_path(relative_path, session.session_id)
            await self._document_service.materialize_page(
                relative_path=incoming_path,
                content=content,
                title=title,
                metadata=metadata,
                overwrite=False,
                backup_existing=False,
            )
            conflict_path = await self._write_conflict_record(
                session=session,
                canonical_path=relative_path,
                incoming_path=incoming_path,
                title=title,
            )
            return {
                "status": "conflict",
                "relative_path": incoming_path,
                "conflict_path": conflict_path,
            }

        await self._document_service.materialize_page(
            relative_path=relative_path,
            content=content,
            title=title,
            metadata=metadata,
            backup_existing=False,
        )
        return {"status": "written", "relative_path": relative_path}

    async def _write_conflict_record(
        self,
        *,
        session: Session,
        canonical_path: str,
        incoming_path: str,
        title: str,
    ) -> str:
        assert self._document_service is not None

        relative_path = conflict_relative_path(title, session.session_id)
        metadata = build_sync_metadata(
            relative_path=relative_path,
            kb_level="L3",
            source_channel=session.channel,
            source_session_id=session.session_id,
            promotion_state="working",
        )
        body = (
            "## 冲突说明\n\n"
            f"- 规范路径: `{canonical_path}`\n"
            f"- 新版本路径: `{incoming_path}`\n"
            f"- session_id: `{session.session_id}`\n"
            f"- source_channel: `{session.channel}`\n\n"
            "## 处理建议\n\n"
            "- 原文与新版本均已保留，请人工合并后再决定正式知识路径。\n"
        )
        content = render_markdown_document(
            title=f"同步冲突 {title}",
            body=body,
            metadata=metadata,
        )
        await self._document_service.materialize_page(
            relative_path=relative_path,
            content=content,
            title=f"同步冲突 {title}",
            metadata=metadata,
            backup_existing=False,
        )
        return relative_path

    async def _append_weekly_summary(
        self,
        session: Session,
        events: list[SessionEvent],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        assert self._document_service is not None

        body = str(summary.get("body_markdown") or "").strip()
        if not body and summary.get("week_focus"):
            focus_items = [
                f"- {item}"
                for item in summary.get("week_focus", [])
                if str(item).strip()
            ]
            body = "\n".join(focus_items)
        if not body:
            return {"status": "skipped"}

        latest_timestamp = events[-1].timestamp if events else session.updated_at
        relative_path = weekly_summary_relative_path(latest_timestamp)
        session_title = str(summary.get("title") or session.summary or f"会话 {session.session_id[:8]}").strip()
        session_anchor = f"session_id: `{session.session_id}`"
        section = (
            f"## {session_title}\n\n"
            f"- session_id: `{session.session_id}`\n"
            f"- source_channel: `{session.channel}`\n\n"
            f"{body.strip()}\n"
        )
        metadata = build_sync_metadata(
            relative_path=relative_path,
            kb_level="L3",
            source_channel=session.channel,
            source_session_id=session.session_id,
            promotion_state="working",
        )

        if self._document_service._content.exists(relative_path):  # noqa: SLF001
            current = self._document_service.read_page(relative_path)
            if session_anchor in current:
                return {"status": "unchanged", "relative_path": relative_path}
            content = current.rstrip() + "\n\n" + section
        else:
            iso_year, iso_week, _ = latest_timestamp.isocalendar()
            content = render_markdown_document(
                title=f"对话周报 {iso_year}-W{iso_week:02d}",
                body="## 本周新增\n\n" + section,
                metadata=metadata,
            )

        await self._document_service.materialize_page(
            relative_path=relative_path,
            content=content,
            title=f"对话周报 {latest_timestamp.isocalendar()[0]}-W{latest_timestamp.isocalendar()[1]:02d}",
            metadata=metadata,
        )
        return {"status": "written", "relative_path": relative_path}

    def _record_medical_kb_promotion(
        self,
        session_id: str,
        *,
        last_event_count: int,
        promoted: bool,
        reason: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if self._session_store is None:
            return
        payload: dict[str, Any] = {
            "last_event_count": last_event_count,
            "promoted": promoted,
            "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if summary:
            payload.update(summary)
        self._session_store.update_session_metadata(
            session_id,
            {"medical_kb_promotion": payload},
        )

    async def reindex_identity_documents(self, *, force: bool = False) -> dict[str, int]:
        """
        将 SOUL.md / USER.md 重新索引到 RetrievalIndex。

        索引策略：
        1. MACHINE SUMMARY 单独作为高密度 identity 文档
        2. 完整文件作为补充文档
        """
        stats = {"documents_indexed": 0, "chunks_created": 0, "errors": 0, "documents_removed": 0}
        identity_files = {
            "soul": self._soul_path,
            "user": self._user_path,
        }

        for kind, path in identity_files.items():
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            summary = self._extract_machine_summary(content) if content else ""
            variants = {
                "summary": self._format_identity_variant(kind, "summary", summary) if summary else "",
                "full": self._format_identity_variant(kind, "full", content) if content else "",
            }
            for variant, variant_content in variants.items():
                source = f"{IDENTITY_SOURCE_PREFIX}{kind}_{variant}"
                try:
                    if not variant_content.strip():
                        await self._retrieval.remove_document(source)
                        stats["documents_removed"] += 1
                        continue
                    chunks = await self._retrieval.index_document(
                        source=source,
                        content=variant_content,
                        metadata={
                            "source": "identity",
                            "identity_kind": kind,
                            "variant": variant,
                            "file_name": path.name,
                            "file_modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat() if path.exists() else "",
                        },
                        force=force,
                    )
                    stats["documents_indexed"] += 1
                    stats["chunks_created"] += chunks
                except Exception:
                    stats["errors"] += 1
                    logger.warning("Failed to index identity document %s (%s)", kind, variant, exc_info=True)
        return stats

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _session_events_to_text(self, session: Session, events: list[SessionEvent]) -> str:
        parts = [
            f"[session] {session.session_id}",
            f"[channel] {session.channel}",
            f"[summary] {session.summary}",
            f"[target_root] {MEDICAL_KB_ROOT}",
        ]
        for event in events:
            role = str(event.role or "").strip() or "unknown"
            content = str(event.content or "").strip()
            if not content:
                continue
            parts.append(f"[{role}] {content[:4000]}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_promoted_body(entry: dict[str, Any]) -> str:
        body = str(entry.get("body_markdown") or "").strip()
        summary = str(entry.get("summary") or "").strip()
        if body:
            return body
        if summary:
            return f"## 摘要\n\n{summary}\n"
        return ""

    def _conflict_variant_path(self, relative_path: str, session_id: str) -> str:
        path = Path(relative_path)
        suffix = path.suffix or ".md"
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        base = path.with_name(f"{path.stem}-incoming-{timestamp}-{session_id[:8]}{suffix}")
        candidate = base.as_posix()
        if self._document_service is None:
            return candidate
        if not self._document_service._content.exists(candidate):  # noqa: SLF001
            return candidate
        for idx in range(1, 1000):
            variant = path.with_name(f"{path.stem}-incoming-{timestamp}-{session_id[:8]}-{idx}{suffix}").as_posix()
            if not self._document_service._content.exists(variant):  # noqa: SLF001
                return variant
        return path.with_name(f"{path.stem}-incoming-{timestamp}-{hashlib.sha1(session_id.encode('utf-8')).hexdigest()[:6]}{suffix}").as_posix()

    @staticmethod
    def _parse_medical_promotion_payload(content: str) -> dict[str, Any]:
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
        raw = json_match.group(1) if json_match else content.strip()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        payload = dict(parsed)
        payload["l2_memories"] = [item for item in payload.get("l2_memories", []) if isinstance(item, dict)]
        payload["l3_entries"] = [item for item in payload.get("l3_entries", []) if isinstance(item, dict)]
        payload["l4_entries"] = [item for item in payload.get("l4_entries", []) if isinstance(item, dict)]
        if not isinstance(payload.get("weekly_summary"), dict):
            payload["weekly_summary"] = {}
        payload["medical_relevant"] = bool(payload.get("medical_relevant"))
        return payload

    def _apply_time_decay(
        self, results: list[RetrievalResult]
    ) -> list[RetrievalResult]:
        """对检索结果应用时间衰减"""
        lambda_val = math.log(2) / self._half_life_days
        now = datetime.now(timezone.utc)

        for r in results:
            indexed_at_str = r.metadata.get("indexed_at") or r.metadata.get("timestamp", "")
            if not indexed_at_str:
                continue
            try:
                # 解析时间戳
                if indexed_at_str.endswith("Z"):
                    indexed_at_str = indexed_at_str[:-1] + "+00:00"
                indexed_at = datetime.fromisoformat(indexed_at_str)
                if indexed_at.tzinfo is None:
                    indexed_at = indexed_at.replace(tzinfo=timezone.utc)
                age_days = (now - indexed_at).total_seconds() / 86400.0
                decay = math.exp(-lambda_val * max(0.0, age_days))
                if str(r.metadata.get("source") or "") == "identity":
                    decay = 1.0
                r.score = r.score * decay
            except (ValueError, TypeError):
                pass  # 无法解析时间，不衰减

        return results

    def _needs_memory_reindex(self) -> bool:
        entries = self._memory._load_entries()
        if not entries:
            return False
        snapshot = self._retrieval.manifest_snapshot()
        indexed = sum(1 for source in snapshot if source.startswith(MEMORY_SOURCE_PREFIX))
        return indexed < len(entries)

    async def _remove_missing_retrieval_sources(self) -> int:
        removed = 0
        snapshot = self._retrieval.manifest_snapshot()
        for source in list(snapshot.keys()):
            if source.startswith(JOURNAL_SOURCE_PREFIX):
                date = source.replace(JOURNAL_SOURCE_PREFIX, "", 1)
                if not (self._journal_dir / f"{date}.md").exists():
                    await self._retrieval.remove_document(source)
                    removed += 1
                continue
            if source.startswith((MEMORY_SOURCE_PREFIX, IDENTITY_SOURCE_PREFIX)):
                continue
            candidate = self._vault_path / source
            if not candidate.exists():
                await self._retrieval.remove_document(source)
                removed += 1
        return removed

    def _normalize_kind(self, kind: str | None) -> str:
        raw = str(kind or "").strip().lower()
        if not raw:
            return "fact"
        return _MEMORY_KIND_ALIASES.get(raw, raw if raw in _DEFAULT_MEMORY_KINDS else "fact")

    def _normalize_tags(self, tags: list[str] | None) -> list[str]:
        if tags is None:
            raw_tags: list[Any] = []
        elif isinstance(tags, (str, bytes)):
            raw_tags = [tags]
        else:
            raw_tags = list(tags)
        cleaned = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
        return sorted(dict.fromkeys(cleaned))

    def _infer_result_kind(self, result: RetrievalResult) -> str:
        if result.source.startswith(MEMORY_SOURCE_PREFIX):
            return self._normalize_kind(str(result.metadata.get("kind") or "fact"))
        if result.source.startswith(IDENTITY_SOURCE_PREFIX):
            return "identity"
        if result.source.startswith(JOURNAL_SOURCE_PREFIX):
            return "journal"
        return self._normalize_kind(str(result.metadata.get("kind") or "fact"))

    @staticmethod
    def _kind_rank(kind: str) -> int:
        order = {
            "preference": 8,
            "decision": 7,
            "workflow_success": 6,
            "workflow_failure": 5,
            "project_state": 4,
            "fact": 3,
            "journal": 2,
            "context": 1,
            "identity": 0,
        }
        return order.get(kind, 0)

    def _format_prompt_memory_item(self, item: dict[str, Any]) -> str:
        content = str(item.get("content") or "").strip()
        if not content:
            return ""
        metadata = item.get("metadata") or {}
        kind = self._normalize_kind(str(metadata.get("kind") or "")) if str(metadata.get("kind") or "") not in {"identity", "journal"} else str(metadata.get("kind"))
        label = _PROMPT_KIND_LABELS.get(kind or "", kind or "记忆")
        timestamp = str(metadata.get("timestamp") or metadata.get("date") or "").strip()
        suffix = f" ({timestamp[:10]})" if timestamp else ""
        compact_content = content.replace("\n", " ")
        return f"- [{label}{suffix}] {self._truncate_text(compact_content, limit=220)}"

    @staticmethod
    def _truncate_text(text: str, *, limit: int) -> str:
        stripped = text.strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: max(0, limit - 3)].rstrip() + "..."

    def _task_signature(self, text: str) -> str:
        tokens = {
            token
            for token in re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", text.lower())
            if len(token) >= 2
        }
        if not tokens:
            return ""
        return " ".join(sorted(tokens)[:8])

    def _build_task_tags(self, task: str, tool_names: list[str]) -> list[str]:
        tokens = re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", task.lower())
        tags = [token for token in tokens if len(token) >= 2][:6]
        tags.extend(tool_names[:4])
        return self._normalize_tags(tags)

    def _suggest_skill_id(self, signature: str, tool_names: list[str]) -> str:
        ascii_tokens = []
        for token in signature.split():
            normalized = re.sub(r"[^a-z0-9]+", "-", token.lower()).strip("-")
            if normalized:
                ascii_tokens.append(normalized)
        for tool_name in tool_names:
            normalized = re.sub(r"[^a-z0-9]+", "-", tool_name.lower()).strip("-")
            if normalized:
                ascii_tokens.append(normalized)
        ascii_tokens = [token for token in ascii_tokens if len(token) >= 2]
        if ascii_tokens:
            return "-".join(dict.fromkeys(ascii_tokens))[:48].strip("-")
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10]
        return f"workflow-{digest}"

    def _extract_tool_statistics(self, events: list[dict[str, Any]] | list[Any]) -> dict[str, list[str]]:
        tool_calls: dict[str, str] = {}
        successful_tools: list[str] = []
        failed_tools: list[str] = []
        artifacts: list[str] = []
        seen_artifacts: set[str] = set()
        for event in events:
            event_type = str(getattr(event, "event_type", "") or "")
            data = getattr(event, "data", {}) or {}
            if event_type == "tool_call":
                call_id = str(data.get("call_id") or "").strip()
                tool_name = str(data.get("tool") or "").strip()
                if call_id and tool_name:
                    tool_calls[call_id] = tool_name
                continue
            if event_type != "tool_result":
                continue
            call_id = str(data.get("call_id") or "").strip()
            tool_name = tool_calls.get(call_id, "")
            if not tool_name:
                continue
            if bool(data.get("success")):
                successful_tools.append(tool_name)
                output = str(data.get("output") or "")
                for match in re.findall(r"([A-Za-z0-9_./-]+\.md)", output):
                    if match not in seen_artifacts:
                        seen_artifacts.add(match)
                        artifacts.append(match)
            else:
                failed_tools.append(tool_name)
        return {
            "successful_tools": self._normalize_tags(successful_tools),
            "failed_tools": self._normalize_tags(failed_tools),
            "artifacts": artifacts,
        }

    def _update_memory_entry_metadata(self, entry_id: str, extra_metadata: dict[str, Any]) -> None:
        entries = self._memory._load_entries()
        updated = False
        for entry in entries:
            if entry.entry_id != entry_id:
                continue
            entry.metadata = {
                **entry.metadata,
                **extra_metadata,
            }
            updated = True
            break
        if updated:
            self._memory._save_entries(entries)

    async def _reindex_memory_entry_by_id(self, entry_id: str) -> None:
        entries = self._memory._load_entries()
        for entry in entries:
            if entry.entry_id == entry_id:
                await self._index_memory_entry(entry)
                return

    @staticmethod
    def _extract_machine_summary(content: str) -> str:
        match = re.search(
            r"^## MACHINE SUMMARY\s+```(?:yaml)?\n(.*?)\n```",
            content,
            re.MULTILINE | re.DOTALL,
        )
        if not match:
            return ""
        return match.group(1).strip()

    @staticmethod
    def _format_identity_variant(kind: str, variant: str, content: str) -> str:
        label = "SOUL.md" if kind == "soul" else "USER.md"
        if variant == "summary":
            return f"{label} MACHINE SUMMARY\n\n{content.strip()}\n"
        return f"{label} FULL CONTENT\n\n{content.strip()}\n"

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        """将 messages 转为文本用于 LLM 提取"""
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if role == "system":
                continue
            if isinstance(content, str) and content:
                parts.append(f"[{role}] {content[:3000]}")
            elif role == "assistant" and msg.get("tool_calls"):
                tool_names = [
                    tc.get("function", {}).get("name", "?")
                    for tc in msg["tool_calls"]
                ]
                parts.append(f"[assistant] Called tools: {', '.join(tool_names)}")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_memory_extraction(content: str) -> list[dict[str, Any]]:
        """从 LLM 响应中解析记忆条目"""
        # 尝试提取 JSON 块
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            text = content.strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [
                    {
                        **m,
                        "kind": _MEMORY_KIND_ALIASES.get(
                            str(m.get("kind") or "").strip().lower(),
                            str(m.get("kind") or "fact").strip().lower() or "fact",
                        ),
                    }
                    for m in parsed
                    if isinstance(m, dict) and m.get("summary")
                ]
            return []
        except (json.JSONDecodeError, TypeError):
            return []
