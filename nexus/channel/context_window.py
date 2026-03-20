"""
Context Window Manager — 上下文窗口管理

职责:
1. 管理 session 的上下文 freshness（消息时效性）
2. 决定 Agent 应该看到哪些历史
3. 上下文压缩策略（参照 OpenClaw 的分级截断）
4. 支持 reset（用户显式重置上下文）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session_store import SessionEvent, SessionStore

logger = logging.getLogger(__name__)

# 1 token ≈ 4 chars (粗估，与 OpenClaw 一致)
_CHARS_PER_TOKEN = 4


class ContextWindowManager:
    """
    管理 session 的上下文窗口。

    核心概念:
    - freshness_minutes: 消息间隔超过此值后，视为 session 已 stale
    - max_events: 上下文窗口最多包含多少条事件
    - max_tokens_estimate: 上下文 token 上限的粗估值

    压缩策略（参照 OpenClaw context-pruning）:
    - soft_trim: 上下文字符数超过窗口 30% 时，截断长消息保留首尾
    - hard_clear: 超过 50% 时，将旧的 tool/assistant 消息替换为占位符
    - 保护最近 keep_last_assistants 条 assistant 消息不被裁剪
    """

    def __init__(
        self,
        session_store: SessionStore,
        freshness_minutes: int = 10,
        max_events: int = 50,
        max_tokens_estimate: int = 8000,
        *,
        # 压缩参数
        context_window_tokens: int = 128_000,
        soft_trim_ratio: float = 0.3,
        hard_clear_ratio: float = 0.5,
        keep_last_assistants: int = 3,
        soft_trim_max_chars: int = 4000,
        soft_trim_head_chars: int = 1500,
        soft_trim_tail_chars: int = 1500,
    ):
        self._store = session_store
        self._freshness_minutes = freshness_minutes
        self._max_events = max_events
        self._max_tokens_estimate = max_tokens_estimate
        # 压缩参数
        self._context_window_chars = context_window_tokens * _CHARS_PER_TOKEN
        self._soft_trim_ratio = soft_trim_ratio
        self._hard_clear_ratio = hard_clear_ratio
        self._keep_last_assistants = keep_last_assistants
        self._soft_trim_max_chars = soft_trim_max_chars
        self._soft_trim_head_chars = soft_trim_head_chars
        self._soft_trim_tail_chars = soft_trim_tail_chars

    def is_within_freshness(
        self, session_id: str, current_time: datetime
    ) -> bool:
        """判断 session 是否仍在 freshness 窗口内"""
        events = self._store.get_events(session_id, limit=1)
        if not events:
            return False

        last_event = events[0]
        delta = current_time - last_event.timestamp
        return delta <= timedelta(minutes=self._freshness_minutes)

    def build_context(self, session_id: str) -> list[dict[str, str]]:
        """
        为 Agent 构建上下文消息列表。

        返回格式与 OpenAI messages API 兼容:
          [{"role": "user", "content": "..."}, ...]

        截断策略:
        1. 最多 max_events 条
        2. 总是保留第一条（任务描述）和最新的消息
        3. 分级压缩长消息（soft-trim → hard-clear）
        """
        events = self._store.get_events(session_id)
        if not events:
            return []

        reset_index = 0
        for idx, event in enumerate(events):
            if event.role == "system" and event.content.startswith("[context_reset]"):
                reset_index = idx + 1
        if reset_index:
            events = events[reset_index:]
            if not events:
                return []

        artifact_message = self._recent_artifacts_message(session_id)

        # 事件数截断：保留首条 + 最近 N 条
        if len(events) > self._max_events:
            first = events[0]
            recent = events[-(self._max_events - 1):]
            events = [first] + recent

        messages = self._events_to_messages(events, artifact_message=artifact_message)

        # 分级压缩
        messages = self._compact_messages(messages)

        return messages

    def reset(self, session_id: str) -> None:
        """
        重置 session 的上下文窗口。

        不删除历史事件，但在 session 中标记一个 reset 点，
        后续 build_context 只返回 reset 点之后的事件。
        """
        self._store.add_event(
            session_id=session_id,
            role="system",
            content="[context_reset] 用户重置了上下文窗口",
        )
        logger.info(f"Context reset for session {session_id}")

    # ------------------------------------------------------------------
    # 分级压缩（参照 OpenClaw context-pruning）
    # ------------------------------------------------------------------

    def _compact_messages(
        self, messages: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """对 messages 执行分级压缩，减少上下文占用。

        Stage 1 (soft-trim): 超过 soft_trim_ratio 时，截断长的 tool/assistant 消息
        Stage 2 (hard-clear): 超过 hard_clear_ratio 时，替换旧消息为占位符
        """
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if total_chars == 0:
            return messages

        ratio = total_chars / self._context_window_chars
        if ratio < self._soft_trim_ratio:
            return messages

        # 标记受保护的索引：第一条 user 消息 + 最近 N 条 assistant 消息
        protected = self._protected_indices(messages)

        # Stage 1: soft-trim
        messages = list(messages)  # shallow copy
        for i, msg in enumerate(messages):
            if i in protected:
                continue
            if msg["role"] not in ("assistant", "tool"):
                continue
            content = msg.get("content", "")
            if len(content) <= self._soft_trim_max_chars:
                continue
            head = content[:self._soft_trim_head_chars]
            tail = content[-self._soft_trim_tail_chars:]
            trimmed = f"{head}\n\n... [已截断 {len(content) - self._soft_trim_head_chars - self._soft_trim_tail_chars} 字符] ...\n\n{tail}"
            messages[i] = {**msg, "content": trimmed}

        # 重新计算
        total_chars = sum(len(m.get("content", "")) for m in messages)
        ratio = total_chars / self._context_window_chars
        if ratio < self._hard_clear_ratio:
            return messages

        # Stage 2: hard-clear（从最旧到最新，跳过受保护的）
        for i, msg in enumerate(messages):
            if i in protected:
                continue
            if msg["role"] not in ("assistant", "tool"):
                continue
            content = msg.get("content", "")
            if len(content) <= 200:
                continue
            messages[i] = {**msg, "content": "[历史回复内容已清理]"}
            total_chars = sum(len(m.get("content", "")) for m in messages)
            ratio = total_chars / self._context_window_chars
            if ratio < self._hard_clear_ratio:
                break

        return messages

    def _protected_indices(self, messages: list[dict[str, str]]) -> set[int]:
        """返回受保护的消息索引（不参与压缩）。

        保护规则:
        - 第一条 user 消息（任务起点）
        - 最近 keep_last_assistants 条 assistant/tool 消息
        """
        protected: set[int] = set()
        # 保护第一条 user 消息
        for i, msg in enumerate(messages):
            if msg["role"] == "user":
                protected.add(i)
                break
        # 保护最近 N 条 assistant 消息
        assistant_indices = [
            i for i, msg in enumerate(messages)
            if msg["role"] in ("assistant", "tool")
        ]
        for idx in assistant_indices[-self._keep_last_assistants:]:
            protected.add(idx)
        return protected

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _events_to_messages(
        events: list[SessionEvent],
        *,
        artifact_message: str = "",
    ) -> list[dict[str, str]]:
        """将 SessionEvent 列表转为 OpenAI 兼容的 messages"""
        messages = []
        if artifact_message:
            messages.append({"role": "system", "content": artifact_message})
        for event in events:
            role = event.role
            if role == "tool":
                role = "assistant"  # 工具结果归入 assistant
            messages.append({"role": role, "content": event.content})
        return messages

    async def compress_with_llm(
        self,
        session_id: str,
        provider: Any,
        model: str | None = None,
    ) -> str:
        """用 LLM 将上次压缩点之后的对话摘要压缩为一条 system 消息。

        流程:
        1. 取出 reset 点之后的所有事件
        2. 调用 LLM 生成摘要
        3. 插入 [context_reset] + 摘要作为新的起点
        4. 后续 build_context 只返回摘要之后的消息

        Returns: 压缩摘要文本
        """
        events = self._store.get_events(session_id)
        if not events:
            return "没有对话历史需要压缩。"

        # 找到 reset 点之后的事件
        reset_index = 0
        for idx, event in enumerate(events):
            if event.role == "system" and event.content.startswith("[context_reset]"):
                reset_index = idx + 1
        if reset_index:
            events = events[reset_index:]
        if len(events) <= 2:
            return "对话历史太短，无需压缩。"

        # 构建对话文本
        conversation_lines = []
        for event in events:
            role_label = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}.get(event.role, event.role)
            content = event.content
            # 截断超长单条消息避免 prompt 爆炸
            if len(content) > 2000:
                content = content[:800] + "\n...[截断]...\n" + content[-800:]
            conversation_lines.append(f"[{role_label}]: {content}")
        conversation_text = "\n\n".join(conversation_lines)
        # 限制总长度
        if len(conversation_text) > 30000:
            conversation_text = conversation_text[:15000] + "\n\n...[中间内容省略]...\n\n" + conversation_text[-15000:]

        compress_prompt = (
            "请将以下对话历史压缩为一份结构化摘要。\n"
            "要求:\n"
            "1. 保留所有关键事实、决策、结论和待办事项\n"
            "2. 保留用户的偏好和明确的指令\n"
            "3. 保留重要的文件路径、配置、代码片段\n"
            "4. 删除寒暄、重复、中间试错过程\n"
            "5. 用中文输出，控制在 1500 字以内\n"
            "6. 格式: 按话题分段，每段一个要点\n\n"
            f"--- 对话历史 ---\n{conversation_text}\n--- 结束 ---"
        )

        try:
            response = await provider.chat_completion(
                model=model or provider.primary_model,
                messages=[
                    {"role": "system", "content": "你是一个对话压缩助手。只输出压缩摘要，不添加任何前言或解释。"},
                    {"role": "user", "content": compress_prompt},
                ],
                temperature=0.2,
                max_tokens=2000,
            )
            summary = response.get("message", {}).get("content", "").strip()
            if not summary:
                return "LLM 返回了空摘要，压缩失败。"
        except Exception as e:
            logger.error(f"Compress LLM call failed: {e}")
            return f"压缩失败: {e}"

        # 插入 reset 点 + 摘要
        self._store.add_event(
            session_id=session_id,
            role="system",
            content="[context_reset] 用户触发了对话压缩",
        )
        self._store.add_event(
            session_id=session_id,
            role="system",
            content=f"[对话摘要]\n{summary}",
        )
        logger.info(f"Context compressed for session {session_id}, summary length: {len(summary)} chars")
        return summary

    def _recent_artifacts_message(self, session_id: str) -> str:
        artifacts = self._store.get_recent_artifacts(session_id, limit=5)
        if not artifacts:
            return ""
        lines = [
            "[session_recent_artifacts]",
            '本会话最近导入了以下附件。若用户提到"刚上传的文件/这个 PDF/这张图片/这个音频"，优先指代这些附件，不要再次向用户索要文件路径。',
        ]
        for item in artifacts:
            parts = [
                f"- {item.get('artifact_type', 'file')} `{item.get('filename') or '未命名附件'}`",
            ]
            relative_path = str(item.get("relative_path") or "").strip()
            page_relative_path = str(item.get("page_relative_path") or "").strip()
            transcript_relative_path = str(item.get("transcript_relative_path") or "").strip()
            if relative_path:
                parts.append(f"原始文件 `{relative_path}`")
            if page_relative_path:
                parts.append(f"知识页 `{page_relative_path}`")
            if transcript_relative_path:
                parts.append(f"转录 `{transcript_relative_path}`")
            lines.append("，".join(parts))
        return "\n".join(lines)
