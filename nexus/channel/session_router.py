"""
Session Router — 会话路由

对标 OpenClaw 的会话管理模型：
- 同一 sender 的消息默认延续活跃会话（session continuity）
- 只有精确命令才做特殊处理
- 不做关键词意图分类，让 LLM 理解用户意图
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from .types import InboundMessage, MessageIntent, RoutingDecision

if TYPE_CHECKING:
    from .context_window import ContextWindowManager
    from .session_store import Session, SessionStore
    from nexus.provider.gateway import ProviderGateway

logger = logging.getLogger(__name__)

# 精确命令词（对标 OpenClaw command detection：只匹配完整消息或 /command 前缀）
_COMMAND_KEYWORDS: dict[str, str] = {
    "停": "pause",
    "暂停": "pause",
    "停一下": "pause",
    "继续": "resume",
    "恢复": "resume",
    "重新开始": "new",
    "新任务": "new",
    "新对话": "new",
    "取消": "cancel",
    "状态": "status",
    "重启": "restart",
    "重启服务": "restart",
    "压缩": "compress",
    "压缩对话": "compress",
    "压缩上下文": "compress",
    "帮助": "help",
    "命令": "help",
}

# 状态查询关键词（功能性：需要查找对应 session）
_STATUS_KEYWORDS = [
    "怎么样了",
    "进展",
    "好了吗",
    "完成了吗",
    "还没回复",
    "还没好吗",
    "到哪一步了",
]

# 历史引用标记（用于决定是否查找历史会话）
_HISTORY_MARKERS = [
    "上次",
    "之前",
    "刚才那个",
]


class SessionRouter:
    """
    简化的会话路由器。

    路由逻辑（对标 OpenClaw session-first routing）：
    1. 附件 → NEW_TASK
    2. 精确命令 → COMMAND
    3. 状态查询 → STATUS_QUERY
    4. 有活跃/新鲜 session → FOLLOW_UP
    5. 显式历史引用 → 查找历史 session
    6. 默认 → NEW_TASK
    """

    CONFIDENCE_THRESHOLD = 0.6

    def __init__(
        self,
        session_store: SessionStore,
        context_window: ContextWindowManager,
        provider: ProviderGateway | None = None,
    ):
        self._store = session_store
        self._context = context_window
        self._provider = provider

    async def route(self, message: InboundMessage) -> RoutingDecision:
        # 附件 → 新任务
        if message.attachments:
            return RoutingDecision(
                intent=MessageIntent.NEW_TASK,
                confidence=0.97,
                reason="message contains attachments",
                metadata={"has_attachments": True},
            )

        # 精确命令匹配
        decision = self._match_command(message)
        if decision:
            return decision

        # 状态查询
        decision = self._match_status_query(message)
        if decision:
            return decision

        # 活跃会话 follow-up（对标 OpenClaw：同 sender 默认延续 session）
        decision = await self._match_active_session(message)
        if decision and decision.confidence >= self.CONFIDENCE_THRESHOLD:
            return decision

        # 显式历史引用 → 查找历史会话
        if self._contains_history_marker(message.content):
            decision = await self._match_historical_session(message)
            if decision:
                return decision

        # 默认：新任务
        return RoutingDecision(
            intent=MessageIntent.NEW_TASK,
            confidence=1.0,
            reason="default new task",
        )

    def _match_command(self, message: InboundMessage) -> RoutingDecision | None:
        text = message.content.strip()
        provider_decision = self._match_provider_command(text)
        if provider_decision is not None:
            return provider_decision
        search_decision = self._match_search_command(text)
        if search_decision is not None:
            return search_decision
        for keyword, action in _COMMAND_KEYWORDS.items():
            if text == keyword or text.startswith(f"/{action}"):
                return RoutingDecision(
                    intent=MessageIntent.COMMAND,
                    confidence=1.0,
                    reason=f"command:{action}",
                    metadata={"action": action},
                )
        return None

    def _match_provider_command(self, text: str) -> RoutingDecision | None:
        normalized = text.strip()
        lowered = normalized.lower()
        if lowered in {"/provider", "/providers", "/provider list", "/provider current"}:
            return RoutingDecision(
                intent=MessageIntent.COMMAND,
                confidence=1.0,
                reason="command:provider",
                metadata={"action": "provider", "provider_command": "status"},
            )

        match = re.fullmatch(
            r"/(?:provider|providers)(?:\s+(?:switch|use|set))?\s+([a-zA-Z0-9._-]+)",
            normalized,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        target = match.group(1).strip().lower()
        return RoutingDecision(
            intent=MessageIntent.COMMAND,
            confidence=1.0,
            reason="command:provider",
            metadata={"action": "provider", "provider_command": "switch", "target": target},
        )

    def _match_search_command(self, text: str) -> RoutingDecision | None:
        normalized = text.strip()
        lowered = normalized.lower()
        if lowered in {"/search", "/search list", "/search current"}:
            return RoutingDecision(
                intent=MessageIntent.COMMAND,
                confidence=1.0,
                reason="command:search",
                metadata={"action": "search_provider", "search_command": "status"},
            )

        match = re.fullmatch(
            r"/search(?:\s+(?:switch|use|set))?\s+([a-zA-Z0-9._-]+)",
            normalized,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        target = match.group(1).strip().lower()
        return RoutingDecision(
            intent=MessageIntent.COMMAND,
            confidence=1.0,
            reason="command:search",
            metadata={"action": "search_provider", "search_command": "switch", "target": target},
        )

    def _match_status_query(self, message: InboundMessage) -> RoutingDecision | None:
        text = message.content.strip()
        if not any(keyword in text for keyword in _STATUS_KEYWORDS):
            return None
        active = self._store.get_active_session(sender_id=message.sender_id)
        if active is not None:
            return RoutingDecision(
                intent=MessageIntent.STATUS_QUERY,
                session_id=active.session_id,
                confidence=0.9,
                reason="status query for active session",
            )
        recent = self._store.get_most_recent_session(sender_id=message.sender_id)
        return RoutingDecision(
            intent=MessageIntent.STATUS_QUERY,
            session_id=recent.session_id if recent else None,
            confidence=0.75,
            reason="status query for most recent session",
        )

    async def _match_active_session(self, message: InboundMessage) -> RoutingDecision | None:
        """对标 OpenClaw：同 sender 的消息默认延续活跃/新鲜 session。"""
        active = self._store.get_active_session(sender_id=message.sender_id)
        candidate = active
        if candidate is None:
            candidate = self._store.get_most_recent_session(sender_id=message.sender_id)
        if candidate is None:
            return None
        if not self._context.is_within_freshness(candidate.session_id, message.timestamp):
            return None

        return RoutingDecision(
            intent=MessageIntent.FOLLOW_UP,
            session_id=candidate.session_id,
            confidence=0.85,
            reason="session continuity (active/fresh session)",
        )

    async def _match_historical_session(self, message: InboundMessage) -> RoutingDecision | None:
        candidates = self._store.find_relevant_sessions(
            sender_id=message.sender_id,
            query=message.content,
            limit=5,
        )
        if not candidates:
            return None

        if len(candidates) == 1:
            return RoutingDecision(
                intent=MessageIntent.RESUME,
                session_id=candidates[0].session_id,
                confidence=0.82,
                reason="single historical candidate",
                metadata={"candidates": [self._render_candidate(candidates[0])]},
            )

        top_candidates = self._dedupe_candidates(candidates)[:3]
        return RoutingDecision(
            intent=MessageIntent.UNKNOWN,
            session_id=None,
            confidence=0.45,
            reason="multiple plausible historical sessions",
            metadata={"candidates": top_candidates},
        )

    @staticmethod
    def _render_candidate(session: Session) -> dict[str, str]:
        summary = SessionRouter._sanitize_candidate_summary(session.summary or "未命名任务")
        return {
            "session_id": session.session_id,
            "summary": summary,
            "updated_at": session.updated_at.isoformat(),
            "status": session.status.value,
        }

    @classmethod
    def _dedupe_candidates(cls, sessions: list[Session]) -> list[dict[str, str]]:
        rendered: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for session in sessions:
            candidate = cls._render_candidate(session)
            normalized_summary = re.sub(r"[，。,.;；:：]+$", "", candidate["summary"].strip().lower())
            key = (
                normalized_summary,
                candidate["status"].strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            rendered.append(candidate)
        return rendered

    @staticmethod
    def _sanitize_candidate_summary(summary: str) -> str:
        text = re.sub(r"\s+", " ", summary.strip())
        text = re.sub(r"^可选项[:：]\s*", "", text)
        text = re.sub(r"^\d+\.\s*", "", text)
        for marker in (
            "可选项：",
            "可选项:",
            "请确认。",
            "我不太确定你要继续哪个任务",
        ):
            idx = text.find(marker)
            if idx > 0:
                text = text[:idx].strip()
        if len(text) > 80:
            text = text[:77].rstrip() + "..."
        return text or "未命名任务"

    @staticmethod
    def _contains_history_marker(text: str) -> bool:
        lowered = text.strip().lower()
        return any(marker in lowered for marker in _HISTORY_MARKERS)
