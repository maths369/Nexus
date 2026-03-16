"""
Streaming — 流式输出适配

职责:
1. 将 LLM 的流式输出适配为不同渠道的格式
2. SSE (Server-Sent Events) 适配
3. WebSocket 适配
4. 渠道无关的 StreamFn 接口
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable, Awaitable

logger = logging.getLogger(__name__)

# StreamFn 类型: 接收文本片段的异步回调
StreamFn = Callable[[str], Awaitable[None]]


class StreamBuffer:
    """
    流式输出缓冲区。

    用于在 Agent 执行过程中收集流式输出，
    并按需分发给一个或多个消费者。
    """

    def __init__(self):
        self._chunks: list[str] = []
        self._subscribers: list[StreamFn] = []
        self._complete = False

    def subscribe(self, callback: StreamFn) -> None:
        """注册流式输出消费者"""
        self._subscribers.append(callback)

    async def push(self, chunk: str) -> None:
        """推送一个文本片段"""
        self._chunks.append(chunk)
        # 分发给所有订阅者
        for sub in self._subscribers:
            try:
                await sub(chunk)
            except Exception as e:
                logger.warning(f"Stream subscriber error: {e}")

    async def complete(self) -> None:
        """标记流式输出完成"""
        self._complete = True

    @property
    def full_text(self) -> str:
        """获取完整的输出文本"""
        return "".join(self._chunks)

    @property
    def is_complete(self) -> bool:
        return self._complete


def create_sse_stream_fn(send_fn: Callable[[str], Awaitable[None]]) -> StreamFn:
    """
    创建 SSE 格式的 StreamFn。

    将每个文本片段包装为 SSE 事件:
      data: {"chunk": "..."}
    """
    async def sse_callback(chunk: str) -> None:
        event_data = json.dumps({"chunk": chunk}, ensure_ascii=False)
        await send_fn(f"data: {event_data}\n\n")
    return sse_callback


def create_ws_stream_fn(send_fn: Callable[[str], Awaitable[None]]) -> StreamFn:
    """
    创建 WebSocket 格式的 StreamFn。

    将每个文本片段包装为 JSON 消息:
      {"type": "stream", "chunk": "..."}
    """
    async def ws_callback(chunk: str) -> None:
        message = json.dumps(
            {"type": "stream", "chunk": chunk},
            ensure_ascii=False,
        )
        await send_fn(message)
    return ws_callback


def create_noop_stream_fn() -> StreamFn:
    """创建空操作的 StreamFn（用于不需要流式输出的场景）"""
    async def noop(chunk: str) -> None:
        pass
    return noop
