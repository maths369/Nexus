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

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .memory import EpisodicMemory, EpisodicMemoryEntry

if TYPE_CHECKING:
    from .retrieval import RetrievalIndex, RetrievalResult
    from nexus.provider.gateway import ProviderGateway

logger = logging.getLogger(__name__)

# 默认时间衰减半衰期（天）
DEFAULT_HALF_LIFE_DAYS = 30.0

# 记忆源前缀，用于在 RetrievalIndex 中区分记忆和文档
MEMORY_SOURCE_PREFIX = "memory://"
IDENTITY_SOURCE_PREFIX = "identity://"


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
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ):
        self._memory = memory
        self._retrieval = retrieval
        self._vault_path = Path(vault_path)
        self._provider = provider
        self._half_life_days = half_life_days

        # 身份文件路径
        self._soul_path = self._vault_path / "_system" / "memory" / "SOUL.md"
        self._user_path = self._vault_path / "_system" / "memory" / "USER.md"
        self._journal_dir = self._vault_path / "_system" / "memory" / "journals"

        # 确保目录存在
        self._soul_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_dir.mkdir(parents=True, exist_ok=True)

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
    ) -> list[dict[str, Any]]:
        """
        语义记忆搜索 — 基于 RetrievalIndex 的混合检索 + 时间衰减。

        与旧的 EpisodicMemory.recall 的区别:
        - 使用 FTS5 + 可选嵌入向量的混合检索（而非纯关键词匹配）
        - 支持时间衰减（近期记忆权重更高）
        - 返回结构化结果（含来源、分数、元数据）
        """
        # 从 RetrievalIndex 搜索 memory:// 源的条目
        all_results = await self._retrieval.search(
            query, top_k=top_k * 3, min_score=0.0
        )

        # 只保留记忆条目
        memory_results = [
            r for r in all_results
            if r.source.startswith(MEMORY_SOURCE_PREFIX)
        ]

        # 应用时间衰减
        if use_time_decay and memory_results:
            memory_results = self._apply_time_decay(memory_results)

        # 过滤最低分并排序
        memory_results = [r for r in memory_results if r.score >= min_score]
        memory_results.sort(key=lambda r: r.score, reverse=True)
        memory_results = memory_results[:top_k]

        # 同时从 EpisodicMemory 做关键词召回作为补充
        episodic_results = await self._memory.recall_entries(query, limit=top_k)
        episodic_ids = {r.source.replace(MEMORY_SOURCE_PREFIX, "") for r in memory_results}

        # 合并去重
        merged: list[dict[str, Any]] = []
        for r in memory_results:
            entry_id = r.source.replace(MEMORY_SOURCE_PREFIX, "")
            merged.append({
                "entry_id": entry_id,
                "content": r.content,
                "score": round(r.score, 4),
                "source": "semantic",
                "metadata": r.metadata,
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
        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged[:top_k]

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
        # 1. 写入 EpisodicMemory
        entry = await self._memory.record(
            kind=kind,
            summary=summary,
            detail=detail,
            tags=tags,
            session_id=session_id,
            importance=importance,
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
                source=f"journal://{today}",
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
                r.score = r.score * decay
            except (ValueError, TypeError):
                pass  # 无法解析时间，不衰减

        return results

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
        import re
        json_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            text = content.strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [
                    m for m in parsed
                    if isinstance(m, dict) and m.get("summary")
                ]
            return []
        except (json.JSONDecodeError, TypeError):
            return []
