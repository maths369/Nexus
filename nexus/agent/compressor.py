"""
Context Compressor — 三层上下文压缩

参考: learn-claude-code s06_context_compact.py

三层压缩管线:
  Layer 1: micro_compact  — 每轮静默替换旧 tool_result 为 placeholder
  Layer 2: auto_compact   — token 超阈值时存 transcript + LLM 总结 → 替换全部 messages
  Layer 3: manual compact — Agent 主动调用 compact tool 触发总结

核心洞见: "Agent 可以策略性遗忘，并继续永久工作。"
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .context import estimate_messages_tokens

if TYPE_CHECKING:
    from nexus.provider.gateway import ProviderGateway

logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_TOKEN_THRESHOLD = 80_000  # 超过此值触发 auto_compact（后备固定值）
KEEP_RECENT_TOOL_RESULTS = 3     # micro_compact 保留最近 N 个 tool_result

# 百分比阈值（借鉴 Claude Code autoCompact 策略）
AUTO_COMPACT_RATIO = 0.80        # 上下文占 80% 时触发压缩
BUFFER_TOKENS = 13_000           # 为 LLM 输出预留的 token 余量

# 断路器: 连续压缩失败 N 次后跳过，防止 API 死循环
MAX_CONSECUTIVE_COMPACT_FAILURES = 3


class ContextCompressor:
    """
    三层上下文压缩器。

    在 tool loop 的每一轮中被调用，负责在 LLM 调用前压缩 messages。
    """

    def __init__(
        self,
        provider: ProviderGateway | None = None,
        transcript_dir: Path | None = None,
        transcript_store: Any | None = None,
        token_threshold: int = DEFAULT_TOKEN_THRESHOLD,
        keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
        summarization_model: str | None = None,
        memory_flush_callback: Any | None = None,
        context_window_tokens: int = 0,
    ):
        self._provider = provider
        self._transcript_dir = transcript_dir
        self._transcript_store = transcript_store
        self._keep_recent = keep_recent
        self._summarization_model = summarization_model
        self._memory_flush_callback = memory_flush_callback
        # 根据模型上下文窗口计算压缩阈值（百分比策略）
        if context_window_tokens > 0:
            self._token_threshold = int(context_window_tokens * AUTO_COMPACT_RATIO) - BUFFER_TOKENS
        else:
            self._token_threshold = token_threshold
        # 统计
        self._micro_compact_count = 0
        self._auto_compact_count = 0
        # 断路器状态
        self._consecutive_compact_failures = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "micro_compact_count": self._micro_compact_count,
            "auto_compact_count": self._auto_compact_count,
            "consecutive_compact_failures": self._consecutive_compact_failures,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "layers": {
                "micro_compact": {
                    "enabled": True,
                    "keep_recent_tool_results": self._keep_recent,
                },
                "auto_compact": {
                    "enabled": True,
                    "token_threshold": self._token_threshold,
                    "summarization_model": self._summarization_model or "qwen-max",
                    "transcript_dir": (
                        str(self._transcript_dir)
                        if self._transcript_dir
                        else str(getattr(self._transcript_store, "base_dir", "")) or None
                    ),
                },
                "manual_compact": {
                    "enabled": True,
                    "tool_name": "compact",
                },
            },
            "stats": self.stats,
        }

    # ------------------------------------------------------------------
    # 主入口: 在每轮 LLM 调用前执行
    # ------------------------------------------------------------------

    async def compress_before_call(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        在每轮 LLM 调用前执行压缩管线。

        修改并返回 messages 列表（可能原地修改，也可能替换为新列表）。
        """
        # Layer 1: micro_compact（每轮静默执行）
        self._micro_compact(messages)

        # Layer 2: auto_compact（超阈值时触发，含断路器保护）
        token_est = estimate_messages_tokens(messages)
        if token_est > self._token_threshold:
            if self._consecutive_compact_failures >= MAX_CONSECUTIVE_COMPACT_FAILURES:
                logger.warning(
                    "Auto-compact skipped: circuit breaker open "
                    "(%d consecutive failures, threshold %d)",
                    self._consecutive_compact_failures,
                    MAX_CONSECUTIVE_COMPACT_FAILURES,
                )
            else:
                logger.info(
                    "Auto-compact triggered: ~%d tokens > threshold %d (%.0f%%)",
                    token_est, self._token_threshold,
                    token_est / max(self._token_threshold, 1) * 100,
                )
                # 压缩前记忆 flush — 避免"压缩即遗忘"
                if self._memory_flush_callback is not None:
                    try:
                        flush_result = await self._memory_flush_callback(messages)
                        logger.info("Memory flush before compact: %s", flush_result)
                    except Exception as e:
                        logger.warning("Memory flush failed: %s", e)
                try:
                    messages = await self._auto_compact(
                        messages,
                        run_id=run_id,
                        session_id=session_id,
                    )
                    # 压缩成功，重置断路器
                    self._consecutive_compact_failures = 0
                except Exception as e:
                    self._consecutive_compact_failures += 1
                    logger.error(
                        "Auto-compact failed (%d/%d): %s",
                        self._consecutive_compact_failures,
                        MAX_CONSECUTIVE_COMPACT_FAILURES,
                        e,
                    )

        return messages

    # ------------------------------------------------------------------
    # Layer 3: 手动压缩（由 compact tool 调用）
    # ------------------------------------------------------------------

    async def manual_compact(
        self,
        messages: list[dict[str, Any]],
        focus: str = "",
        *,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        手动触发压缩，与 auto_compact 相同逻辑，但可指定保留重点。
        """
        logger.info("Manual compact triggered, focus=%s", focus[:80])
        return await self._auto_compact(
            messages,
            focus=focus,
            run_id=run_id,
            session_id=session_id,
        )

    # ------------------------------------------------------------------
    # Layer 1: micro_compact
    # ------------------------------------------------------------------

    def _micro_compact(self, messages: list[dict[str, Any]]) -> None:
        """
        静默替换旧的 tool_result 内容为 placeholder。

        只保留最近 N 个 tool_result 的完整内容，
        更早的替换为 "[Previous: used {tool_name}]"。
        """
        # 收集所有 tool message 的索引
        tool_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool":
                tool_indices.append(idx)

        if len(tool_indices) <= self._keep_recent:
            return

        # 构建 tool_call_id → tool_name 的映射
        tool_name_map = self._build_tool_name_map(messages)

        # 清理旧的 tool results
        to_clear = tool_indices[:-self._keep_recent]
        cleared = 0
        for idx in to_clear:
            msg = messages[idx]
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 100:
                call_id = msg.get("tool_call_id", "")
                tool_name = tool_name_map.get(call_id, "tool")
                msg["content"] = f"[Previous: used {tool_name}]"
                cleared += 1

        if cleared > 0:
            self._micro_compact_count += cleared
            logger.debug("micro_compact: cleared %d old tool results", cleared)

    @staticmethod
    def _build_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
        """从 assistant 消息中提取 tool_call_id → tool_name 映射"""
        mapping: dict[str, str] = {}
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                name = func.get("name", "unknown")
                if tc_id:
                    mapping[tc_id] = name
        return mapping

    # ------------------------------------------------------------------
    # Layer 2: auto_compact
    # ------------------------------------------------------------------

    async def _auto_compact(
        self,
        messages: list[dict[str, Any]],
        focus: str = "",
        *,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        保存完整 transcript → LLM 总结 → 替换为 [summary]。

        返回新的 messages 列表（2 条: compressed summary + ack）。
        """
        # 保存 transcript
        transcript_path = self._save_transcript(
            messages,
            run_id=run_id,
            session_id=session_id,
        )

        # 提取 system message（总是保留）
        system_msg = None
        for msg in messages:
            if msg.get("role") == "system":
                system_msg = msg
                break

        # LLM 总结
        summary = await self._summarize(messages, focus)

        self._auto_compact_count += 1

        # 构建压缩后的 messages
        compressed: list[dict[str, Any]] = []
        if system_msg:
            compressed.append(system_msg)

        transcript_ref = f" Transcript: {transcript_path}" if transcript_path else ""
        compressed.append({
            "role": "user",
            "content": (
                f"[上下文已压缩。{transcript_ref}]\n\n"
                f"{summary}"
            ),
        })
        compressed.append({
            "role": "assistant",
            "content": "好的，我已理解之前的上下文摘要，继续处理。",
        })

        logger.info(
            "auto_compact: %d messages → %d, transcript=%s",
            len(messages), len(compressed), transcript_path,
        )
        return compressed

    async def _summarize(
        self, messages: list[dict[str, Any]], focus: str = "",
    ) -> str:
        """用 LLM 生成对话摘要"""
        # 如果没有 provider，用规则摘要
        if not self._provider:
            return self._append_missing_identifiers(messages, self._rule_based_summary(messages))

        # 构建总结请求
        conversation_text = self._messages_to_text(messages)
        # 截断以防对话本身就超长
        if len(conversation_text) > 60_000:
            conversation_text = conversation_text[:30_000] + "\n...(中间省略)...\n" + conversation_text[-30_000:]

        focus_instruction = f"\n特别关注: {focus}" if focus else ""

        try:
            model = self._summarization_model or "qwen-max"
            response = await self._provider.chat_completion(
                model=model,
                messages=[{
                    "role": "user",
                    "content": (
                        "请为以下对话生成延续性摘要，包括：\n"
                        "1) 已完成的工作\n"
                        "2) 当前状态和进展\n"
                        "3) 关键决策和结论\n"
                        "4) 待处理的事项\n"
                        "5) 必须保留所有关键标识符：文件路径、函数名、变量名、task id、run id、session id。\n"
                        "简洁但保留关键细节。"
                        f"{focus_instruction}\n\n"
                        f"{conversation_text}"
                    ),
                }],
                max_tokens=2000,
                temperature=0.3,
            )
            assistant_msg = response.get("message", {})
            summary = assistant_msg.get("content", "") or self._rule_based_summary(messages)
            return self._append_missing_identifiers(messages, summary)
        except Exception as e:
            logger.warning("LLM summarization failed: %s, using rule-based", e)
            return self._append_missing_identifiers(messages, self._rule_based_summary(messages))

    def _save_transcript(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str | None:
        """保存完整 transcript 到磁盘"""
        transcript_id = session_id or run_id or f"transcript_{int(time.time() * 1000)}"
        if self._transcript_store is not None:
            try:
                return self._transcript_store.append_snapshot(
                    transcript_id,
                    messages,
                    trigger="compact",
                    metadata={"run_id": run_id, "session_id": session_id},
                )
            except Exception as e:
                logger.warning("Failed to save transcript via TranscriptStore: %s", e)
        if not self._transcript_dir:
            return None

        self._transcript_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in transcript_id)
        path = self._transcript_dir / f"{safe_id}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "snapshot",
                    "timestamp": datetime.now().isoformat(),
                    "run_id": run_id,
                    "session_id": session_id,
                    "message_count": len(messages),
                }, ensure_ascii=False, default=str) + "\n")
                for msg in messages:
                    f.write(json.dumps({"kind": "message", "message": msg}, ensure_ascii=False, default=str) + "\n")
            return str(path)
        except Exception as e:
            logger.warning("Failed to save transcript: %s", e)
            return None

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        """将 messages 转为可读文本用于总结"""
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if role == "system":
                continue  # 不需要总结 system prompt
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
    def _rule_based_summary(messages: list[dict[str, Any]]) -> str:
        """无 LLM 时的规则摘要：提取 user 消息和 assistant 文本回复"""
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        tool_count = 0

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str) and not content.startswith("["):
                user_msgs.append(content[:200])
            elif role == "assistant" and isinstance(content, str) and content:
                assistant_msgs.append(content[:200])
            elif role == "tool":
                tool_count += 1

        lines = ["## 上下文摘要（规则生成）"]
        if user_msgs:
            lines.append(f"用户请求: {'; '.join(user_msgs[:5])}")
        if assistant_msgs:
            lines.append(f"助手回复: {'; '.join(assistant_msgs[-3:])}")
        if tool_count:
            lines.append(f"共执行了 {tool_count} 次工具调用")
        return "\n".join(lines)

    @staticmethod
    def _extract_identifiers(messages: list[dict[str, Any]]) -> list[str]:
        text = ContextCompressor._messages_to_text(messages)
        patterns = [
            r"`([^`]{2,120})`",
            r"\b(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+\b",
            r"\b(?:task|run|session|sub)-[A-Za-z0-9_-]+\b",
            r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b",
        ]
        candidates: list[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, text):
                identifier = match if isinstance(match, str) else ""
                if not identifier:
                    continue
                if identifier.lower() in {"assistant", "system", "user", "tool", "called", "tools"}:
                    continue
                if len(identifier) > 120:
                    continue
                candidates.append(identifier)
        ordered: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered[:40]

    @classmethod
    def _append_missing_identifiers(
        cls,
        messages: list[dict[str, Any]],
        summary: str,
    ) -> str:
        identifiers = cls._extract_identifiers(messages)
        if not identifiers:
            return summary
        missing = [identifier for identifier in identifiers if identifier not in summary]
        if not missing:
            return summary
        return summary.rstrip() + "\n\n关键标识符:\n- " + "\n- ".join(missing[:20])
