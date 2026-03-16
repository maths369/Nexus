"""
Context Window Manager — 上下文窗口管理

职责:
1. 管理 session 的上下文 freshness（消息时效性）
2. 决定 Agent 应该看到哪些历史
3. 上下文截断策略
4. 支持 reset（用户显式重置上下文）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session_store import SessionEvent, SessionStore

logger = logging.getLogger(__name__)


class ContextWindowManager:
    """
    管理 session 的上下文窗口。

    核心概念:
    - freshness_minutes: 消息间隔超过此值后，视为 session 已 stale
    - max_events: 上下文窗口最多包含多少条事件
    - max_tokens_estimate: 上下文 token 上限的粗估值
    """

    def __init__(
        self,
        session_store: SessionStore,
        freshness_minutes: int = 10,
        max_events: int = 50,
        max_tokens_estimate: int = 8000,
    ):
        self._store = session_store
        self._freshness_minutes = freshness_minutes
        self._max_events = max_events
        self._max_tokens_estimate = max_tokens_estimate

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
        2. 粗估 token 不超过 max_tokens_estimate
        3. 总是保留第一条（任务描述）和最新的消息
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

        # 如果事件数未超过限制，直接返回
        if len(events) <= self._max_events:
            return self._events_to_messages(events, artifact_message=artifact_message)

        # 截断：保留首条 + 最近 N 条
        first = events[0]
        recent = events[-(self._max_events - 1):]
        truncated = [first] + recent

        return self._events_to_messages(truncated, artifact_message=artifact_message)

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

    def _recent_artifacts_message(self, session_id: str) -> str:
        artifacts = self._store.get_recent_artifacts(session_id, limit=5)
        if not artifacts:
            return ""
        lines = [
            "[session_recent_artifacts]",
            "本会话最近导入了以下附件。若用户提到“刚上传的文件/这个 PDF/这张图片/这个音频”，优先指代这些附件，不要再次向用户索要文件路径。",
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
