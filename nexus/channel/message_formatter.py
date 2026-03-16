"""
Message Formatter — 回复格式化

职责:
1. 将 Agent 的输出格式化为渠道友好的消息
2. 生成接收确认、状态更新、阻塞提示、最终结果
3. 适配不同渠道的格式要求（飞书卡片 vs Web HTML）
"""

from __future__ import annotations

import logging
from typing import Any

from .types import OutboundMessage, OutboundMessageType

logger = logging.getLogger(__name__)


class MessageFormatter:
    """
    消息格式化器。

    设计原则:
    1. 默认自然语言优先
    2. 内部治理状态不直接外泄给用户
    3. 只在必要时展示技术细节
    """

    def format_ack(self, session_id: str, task_summary: str) -> OutboundMessage:
        """生成接收确认消息"""
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.ACK,
            content=f"收到，正在处理：{task_summary}",
        )

    def format_status(
        self, session_id: str, status: str, progress: str = ""
    ) -> OutboundMessage:
        """生成状态更新消息"""
        content = f"当前状态：{status}"
        if progress:
            content += f"\n{progress}"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.STATUS,
            content=content,
        )

    def format_blocked(
        self, session_id: str, reason: str, options: list[str] | None = None
    ) -> OutboundMessage:
        """生成阻塞提示消息"""
        content = f"需要你的确认：{reason}"
        if options:
            content += "\n\n选项：\n"
            for i, opt in enumerate(options, 1):
                content += f"  {i}. {opt}\n"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.BLOCKED,
            content=content,
        )

    def format_result(
        self,
        session_id: str,
        result: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> OutboundMessage:
        """生成最终结果消息"""
        content = result
        if artifacts:
            content += "\n\n📎 相关文件：\n"
            for artifact in artifacts:
                name = artifact.get("name", "未知文件")
                path = artifact.get("path", "")
                content += f"  • {name}: {path}\n"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.RESULT,
            content=content,
        )

    def format_clarify(
        self,
        session_id: str,
        question: str,
        options: list[str] | None = None,
    ) -> OutboundMessage:
        """生成澄清请求消息"""
        content = question.strip()
        if options:
            content += "\n\n可选项：\n"
            for idx, option in enumerate(options, start=1):
                content += f"{idx}. {option}\n"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.CLARIFY,
            content=content.rstrip(),
        )

    def format_error(
        self, session_id: str, error: str, user_friendly: bool = True
    ) -> OutboundMessage:
        """生成错误通知消息"""
        if user_friendly:
            content = f"处理过程中遇到了问题：{error}\n\n你可以重试或换一种方式描述你的需求。"
        else:
            content = f"错误：{error}"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.ERROR,
            content=content,
        )
