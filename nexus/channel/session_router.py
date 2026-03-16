"""Session Router — classify inbound messages into new task, follow-up, resume, status, or command."""

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

_COMMAND_KEYWORDS: dict[str, str] = {
    "停": "pause",
    "暂停": "pause",
    "停一下": "pause",
    "继续": "resume",
    "恢复": "resume",
    "重新开始": "restart",
    "取消": "cancel",
    "状态": "status",
}

_STATUS_KEYWORDS = [
    "怎么样了",
    "进展",
    "好了吗",
    "完成了吗",
    "还没回复",
    "还没好吗",
    "到哪一步了",
    "状态",
]

_HISTORY_MARKERS = [
    "上次",
    "之前",
    "刚才那个",
    "那个",
    "这个",
    "继续",
    "接着",
    "恢复",
]

_NEW_TASK_MARKERS = [
    "新问题",
    "新任务",
    "另一个",
    "另外",
    "顺便",
    "再帮我",
    "帮我",
    "请你",
    "麻烦你",
    "我希望你",
    "我想让你",
    "希望你",
]

_FOLLOW_UP_MARKERS = [
    "补充",
    "细化",
    "展开",
    "修改",
    "更新",
    "继续",
    "接着",
    "基于这个",
    "在此基础上",
]

_CAPABILITY_ACTION_MARKERS = [
    "安装",
    "启用",
    "开通",
    "增加",
    "新增",
    "获得",
]

_CAPABILITY_TARGET_MARKERS = [
    "能力",
    "skill",
    "skills",
    "capability",
    "工具",
]

_INVENTORY_ACTION_MARKERS = [
    "列出",
    "有哪些",
    "有哪",
    "给我看看",
    "盘点",
    "汇总",
    "统计",
]

_INVENTORY_TARGET_MARKERS = [
    "pdf",
    "文件",
    "附件",
    "图片",
    "日志",
    "会议",
    "知识库",
    "技能",
    "能力",
    "工具",
]


class SessionRouter:
    """Low-bloat router with explicit clarification instead of silent guessing."""

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
        explicit_history = self._contains_history_marker(message.content)

        if message.attachments and not self._contains_history_marker(message.content):
            return RoutingDecision(
                intent=MessageIntent.NEW_TASK,
                confidence=0.97,
                reason="message contains attachments",
                metadata={"has_attachments": True},
            )

        decision = self._match_command(message)
        if decision:
            return decision

        decision = self._match_status_query(message)
        if decision:
            return decision

        if self._is_explicit_new_task(message.content) and not explicit_history:
            return RoutingDecision(
                intent=MessageIntent.NEW_TASK,
                confidence=0.95,
                reason="explicit new-task marker",
            )

        if self._is_capability_install_request(message.content) and not explicit_history:
            return RoutingDecision(
                intent=MessageIntent.NEW_TASK,
                confidence=0.96,
                reason="capability install request",
            )

        if self._is_inventory_query(message.content) and not explicit_history:
            return RoutingDecision(
                intent=MessageIntent.NEW_TASK,
                confidence=0.94,
                reason="inventory query",
            )

        decision = await self._match_active_session(message)
        if decision and decision.confidence >= self.CONFIDENCE_THRESHOLD:
            return decision

        # Align with OpenClaw's session-first routing:
        # only consult historical sessions when the user explicitly refers to history.
        if explicit_history:
            decision = await self._match_historical_session(message)
            if decision:
                return decision

        return RoutingDecision(
            intent=MessageIntent.NEW_TASK,
            confidence=1.0,
            reason="default new task after session-first routing",
        )

    def _match_command(self, message: InboundMessage) -> RoutingDecision | None:
        text = message.content.strip()
        for keyword, action in _COMMAND_KEYWORDS.items():
            if text == keyword or text.startswith(f"/{action}"):
                return RoutingDecision(
                    intent=MessageIntent.COMMAND,
                    confidence=1.0,
                    reason=f"command:{action}",
                    metadata={"action": action},
                )
        return None

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
        active = self._store.get_active_session(sender_id=message.sender_id)
        if not active:
            return None
        if not self._context.is_within_freshness(active.session_id, message.timestamp):
            return None

        text = message.content.strip()
        if self._looks_like_follow_up(text, active):
            return RoutingDecision(
                intent=MessageIntent.FOLLOW_UP,
                session_id=active.session_id,
                confidence=0.88,
                reason="fresh active session follow-up",
            )

        if self._is_brief_ambiguous_reply(text):
            return RoutingDecision(
                intent=MessageIntent.FOLLOW_UP,
                session_id=active.session_id,
                confidence=0.65,
                reason="brief reply within active freshness window",
            )
        return None

    async def _match_historical_session(self, message: InboundMessage) -> RoutingDecision | None:
        candidates = self._store.find_relevant_sessions(
            sender_id=message.sender_id,
            query=message.content,
            limit=5,
        )
        if not candidates:
            return None

        explicit_history = self._contains_history_marker(message.content)
        if len(candidates) == 1:
            return RoutingDecision(
                intent=MessageIntent.RESUME,
                session_id=candidates[0].session_id,
                confidence=0.82 if explicit_history else 0.66,
                reason="single historical candidate",
                metadata={"candidates": [self._render_candidate(candidates[0])]},
            )

        # Multiple plausible sessions: clarify instead of guessing.
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
        return any(marker in lowered for marker in _STATUS_KEYWORDS + _HISTORY_MARKERS)

    @staticmethod
    def _is_explicit_new_task(text: str) -> bool:
        lowered = text.strip().lower()
        if any(marker in lowered for marker in _NEW_TASK_MARKERS):
            return True
        if len(lowered) >= 8 and lowered.startswith("帮我"):
            return True
        return False

    @staticmethod
    def _is_capability_install_request(text: str) -> bool:
        lowered = text.strip().lower()
        if not lowered:
            return False
        has_action = any(marker in lowered for marker in _CAPABILITY_ACTION_MARKERS)
        has_target = any(marker in lowered for marker in _CAPABILITY_TARGET_MARKERS)
        return has_action and has_target

    @staticmethod
    def _is_brief_ambiguous_reply(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return 0 < len(compact) <= 12 and not any(token in compact for token in _NEW_TASK_MARKERS)

    @staticmethod
    def _is_inventory_query(text: str) -> bool:
        lowered = text.strip().lower()
        if not lowered:
            return False
        has_action = any(marker in lowered for marker in _INVENTORY_ACTION_MARKERS)
        has_target = any(marker in lowered for marker in _INVENTORY_TARGET_MARKERS)
        return has_action and has_target

    @staticmethod
    def _looks_like_follow_up(text: str, session: Session) -> bool:
        lowered = text.strip().lower()
        if any(marker in lowered for marker in _FOLLOW_UP_MARKERS):
            return True
        if SessionRouter._contains_history_marker(lowered):
            return True
        summary = (session.summary or "").strip().lower()
        if summary and summary in lowered:
            return True
        return False
