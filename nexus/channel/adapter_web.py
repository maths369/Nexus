"""
Web Channel Adapter — Web WebSocket 适配

职责:
1. WebSocket 连接管理
2. 消息接收与解析
3. 流式响应推送
4. 心跳与重连
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .types import ChannelType, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class WebAdapter:
    """
    Web 渠道适配器。

    通过 WebSocket 与 Web 前端通信。
    支持流式响应推送（SSE 风格的增量内容推送）。
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or {}
        # WebSocket 连接管理
        self._connections: dict[str, Any] = {}

    async def handle_ws_message(
        self, ws_id: str, raw_message: str
    ) -> InboundMessage | None:
        """
        处理 WebSocket 消息。

        消息格式:
        {
            "type": "message",
            "content": "用户输入的文本",
            "sender_id": "web_user_xxx",
            "attachments": [...]
        }
        """
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from WS {ws_id}: {raw_message[:100]}")
            return None

        msg_type = data.get("type", "")
        if msg_type != "message":
            logger.debug(f"Non-message WS event: {msg_type}")
            return None

        return InboundMessage(
            message_id=f"web_{ws_id}_{data.get('seq', 0)}",
            channel=ChannelType.WEB,
            sender_id=data.get("sender_id", f"web_user_{ws_id}"),
            content=data.get("content", ""),
            attachments=data.get("attachments", []),
        )

    async def send_message(
        self, ws_id: str, message: OutboundMessage
    ) -> None:
        """向 WebSocket 客户端发送消息"""
        payload = {
            "type": message.message_type.value,
            "session_id": message.session_id,
            "content": message.content,
            "metadata": message.metadata,
        }
        # TODO: 实际 WebSocket 发送
        logger.info(
            f"WS send to {ws_id}: {message.message_type.value} "
            f"({len(message.content)} chars)"
        )

    async def send_stream_chunk(
        self, ws_id: str, session_id: str, chunk: str
    ) -> None:
        """向 WebSocket 客户端推送流式内容片段"""
        payload = {
            "type": "stream",
            "session_id": session_id,
            "chunk": chunk,
        }
        # TODO: 实际 WebSocket 发送
        logger.debug(f"WS stream to {ws_id}: {len(chunk)} chars")
