"""
Mesh Transport — 节点间通信层

抽象通信接口，支持：
1. InMemoryTransport — 用于测试和单机多节点模拟
2. MQTTTransport — 生产环境基于 MQTT 5.0 的通信（Phase 0 实现）

Topic 设计:
  nexus/nodes/{node_id}/heartbeat     — 心跳
  nexus/nodes/{node_id}/card          — 节点注册/能力变更
  nexus/nodes/{node_id}/offline       — 节点下线
  nexus/tasks/{task_id}/assign        — 任务分配
  nexus/tasks/{task_id}/status        — 任务状态更新
  nexus/tasks/{task_id}/result        — 任务结果
  nexus/rpc/{node_id}/{request_id}    — RPC 请求
  nexus/rpc/{node_id}/{request_id}/response — RPC 响应
  nexus/broadcast/+                   — 全局广播
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import ssl
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class MessageType(str, enum.Enum):
    """消息类型"""
    NODE_REGISTER = "node_register"
    NODE_HEARTBEAT = "node_heartbeat"
    NODE_OFFLINE = "node_offline"
    CAPABILITY_UPDATE = "capability_update"
    TASK_ASSIGN = "task_assign"
    TASK_STATUS = "task_status"
    TASK_RESULT = "task_result"
    RPC_REQUEST = "rpc_request"
    RPC_RESPONSE = "rpc_response"
    BROADCAST = "broadcast"


@dataclass
class MeshMessage:
    """节点间消息"""
    message_id: str
    message_type: MessageType
    source_node: str
    target_node: str              # "" = broadcast
    topic: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {
                "message_id": self.message_id,
                "message_type": self.message_type.value,
                "source_node": self.source_node,
                "target_node": self.target_node,
                "topic": self.topic,
                "payload": self.payload,
                "timestamp": self.timestamp,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> MeshMessage:
        data = json.loads(text)
        return cls(
            message_id=data.get("message_id", ""),
            message_type=MessageType(data.get("message_type", "broadcast")),
            source_node=data.get("source_node", ""),
            target_node=data.get("target_node", ""),
            topic=data.get("topic", ""),
            payload=data.get("payload", {}),
            timestamp=float(data.get("timestamp") or time.time()),
        )


# 订阅回调类型: (topic, message) -> None
SubscriptionCallback = Callable[[str, MeshMessage], Awaitable[None]]


def _topic_matches(pattern: str, topic: str) -> bool:
    """MQTT 风格的 topic 匹配。"""
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")

    pi = 0
    ti = 0
    while pi < len(pattern_parts) and ti < len(topic_parts):
        if pattern_parts[pi] == "#":
            return True
        if pattern_parts[pi] == "+" or pattern_parts[pi] == topic_parts[ti]:
            pi += 1
            ti += 1
        else:
            return False

    return pi == len(pattern_parts) and ti == len(topic_parts)


def _normalize_payload(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    if isinstance(payload, bytearray):
        return bytes(payload).decode("utf-8")
    if isinstance(payload, str):
        return payload
    return str(payload)


def _load_aiomqtt():
    try:
        import aiomqtt  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on runtime environment
        raise RuntimeError(
            "MQTTTransport requires aiomqtt. Install project dependencies in the ai_assist environment."
        ) from exc
    return aiomqtt


def _build_tls_context(
    *,
    enabled: bool,
    ca_path: str | None,
    cert_path: str | None,
    key_path: str | None,
    insecure: bool,
) -> ssl.SSLContext | None:
    if not enabled:
        return None

    context = ssl.create_default_context()
    if ca_path:
        context.load_verify_locations(ca_path)
    if cert_path:
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class MeshTransport:
    """
    通信层抽象基类。

    子类实现 publish/subscribe/unsubscribe/request 即可。
    """

    async def connect(self) -> None:
        """连接到消息代理"""
        raise NotImplementedError

    @property
    def node_id(self) -> str:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    async def disconnect(self) -> None:
        """断开连接"""
        raise NotImplementedError

    async def publish(self, topic: str, message: MeshMessage) -> None:
        """发布消息到指定 topic"""
        raise NotImplementedError

    async def subscribe(
        self,
        topic_pattern: str,
        callback: SubscriptionCallback,
    ) -> None:
        """
        订阅 topic 模式。

        支持通配符:
        - + 匹配一层
        - # 匹配多层
        """
        raise NotImplementedError

    async def unsubscribe(self, topic_pattern: str) -> None:
        """取消订阅"""
        raise NotImplementedError

    async def request(
        self,
        target_node: str,
        payload: dict[str, Any],
        *,
        timeout: float = 30.0,
        source_node: str = "",
    ) -> MeshMessage:
        """
        RPC 请求-响应。

        发送请求到目标节点并等待响应。
        """
        raise NotImplementedError

    def make_message(
        self,
        message_type: MessageType,
        topic: str,
        payload: dict[str, Any],
        *,
        target_node: str = "",
    ) -> MeshMessage:
        """创建一条消息的便捷方法"""
        return MeshMessage(
            message_id=uuid.uuid4().hex[:16],
            message_type=message_type,
            source_node=self.node_id,
            target_node=target_node,
            topic=topic,
            payload=payload,
        )


class InMemoryTransport(MeshTransport):
    """
    内存中的消息传输实现。

    用于测试和单机开发。支持多个节点在同一进程中通信。
    所有 InMemoryTransport 实例共享同一个 _Hub。
    """

    class _Hub:
        """共享消息总线"""

        def __init__(self) -> None:
            self._subscriptions: dict[str, list[tuple[str, SubscriptionCallback]]] = {}
            # topic_pattern -> [(subscriber_id, callback)]
            self._pending_rpc: dict[str, asyncio.Future[MeshMessage]] = {}

        async def publish(self, topic: str, message: MeshMessage) -> None:
            """将消息分发给所有匹配的订阅者"""
            for pattern, subscribers in self._subscriptions.items():
                if self._topic_matches(pattern, topic):
                    for subscriber_id, callback in subscribers:
                        if subscriber_id != message.source_node or pattern.startswith("nexus/broadcast"):
                            try:
                                await callback(topic, message)
                            except Exception:
                                logger.warning(
                                    "Subscription callback failed: topic=%s subscriber=%s",
                                    topic,
                                    subscriber_id,
                                    exc_info=True,
                                )

            # 检查是否有 RPC 响应等待
            if message.message_type == MessageType.RPC_RESPONSE:
                request_id = message.payload.get("request_id", "")
                future = self._pending_rpc.pop(request_id, None)
                if future and not future.done():
                    future.set_result(message)

        def subscribe(
            self,
            subscriber_id: str,
            topic_pattern: str,
            callback: SubscriptionCallback,
        ) -> None:
            if topic_pattern not in self._subscriptions:
                self._subscriptions[topic_pattern] = []
            self._subscriptions[topic_pattern].append((subscriber_id, callback))

        def unsubscribe(self, subscriber_id: str, topic_pattern: str) -> None:
            if topic_pattern in self._subscriptions:
                self._subscriptions[topic_pattern] = [
                    (sid, cb)
                    for sid, cb in self._subscriptions[topic_pattern]
                    if sid != subscriber_id
                ]

        def register_rpc(self, request_id: str, future: asyncio.Future[MeshMessage]) -> None:
            self._pending_rpc[request_id] = future

        def cancel_rpc(self, request_id: str) -> None:
            future = self._pending_rpc.pop(request_id, None)
            if future and not future.done():
                future.cancel()

        @staticmethod
        def _topic_matches(pattern: str, topic: str) -> bool:
            """MQTT 风格的 topic 匹配"""
            return _topic_matches(pattern, topic)

    # 所有 InMemoryTransport 实例共享的 Hub 注册表
    _hubs: dict[str, _Hub] = {}

    def __init__(self, node_id: str, *, hub_name: str = "default") -> None:
        self._node_id = node_id
        self._hub_name = hub_name
        self._connected = False

    @classmethod
    def _get_hub(cls, hub_name: str) -> _Hub:
        if hub_name not in cls._hubs:
            cls._hubs[hub_name] = cls._Hub()
        return cls._hubs[hub_name]

    @classmethod
    def reset_hub(cls, hub_name: str = "default") -> None:
        """重置共享 Hub（测试用）"""
        cls._hubs.pop(hub_name, None)

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True
        logger.debug("InMemoryTransport connected: node=%s hub=%s", self._node_id, self._hub_name)

    async def disconnect(self) -> None:
        self._connected = False
        logger.debug("InMemoryTransport disconnected: node=%s", self._node_id)

    async def publish(self, topic: str, message: MeshMessage) -> None:
        if not self._connected:
            raise RuntimeError(f"Transport not connected: {self._node_id}")
        hub = self._get_hub(self._hub_name)
        await hub.publish(topic, message)

    async def subscribe(
        self,
        topic_pattern: str,
        callback: SubscriptionCallback,
    ) -> None:
        if not self._connected:
            raise RuntimeError(f"Transport not connected: {self._node_id}")
        hub = self._get_hub(self._hub_name)
        hub.subscribe(self._node_id, topic_pattern, callback)

    async def unsubscribe(self, topic_pattern: str) -> None:
        hub = self._get_hub(self._hub_name)
        hub.unsubscribe(self._node_id, topic_pattern)

    async def request(
        self,
        target_node: str,
        payload: dict[str, Any],
        *,
        timeout: float = 30.0,
        source_node: str = "",
    ) -> MeshMessage:
        if not self._connected:
            raise RuntimeError(f"Transport not connected: {self._node_id}")

        request_id = uuid.uuid4().hex[:12]
        hub = self._get_hub(self._hub_name)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MeshMessage] = loop.create_future()
        hub.register_rpc(request_id, future)

        # 发送 RPC 请求
        message = MeshMessage(
            message_id=uuid.uuid4().hex[:16],
            message_type=MessageType.RPC_REQUEST,
            source_node=source_node or self._node_id,
            target_node=target_node,
            topic=f"nexus/rpc/{target_node}/{request_id}",
            payload={**payload, "request_id": request_id},
        )
        await self.publish(message.topic, message)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            hub.cancel_rpc(request_id)
            raise TimeoutError(
                f"RPC request to {target_node} timed out after {timeout}s"
            ) from None

    def make_message(
        self,
        message_type: MessageType,
        topic: str,
        payload: dict[str, Any],
        *,
        target_node: str = "",
    ) -> MeshMessage:
        """创建一条消息的便捷方法"""
        return MeshMessage(
            message_id=uuid.uuid4().hex[:16],
            message_type=message_type,
            source_node=self._node_id,
            target_node=target_node,
            topic=topic,
            payload=payload,
        )


class MQTTTransport(MeshTransport):
    """生产环境 MQTT 传输实现。"""

    def __init__(
        self,
        node_id: str,
        *,
        hostname: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        transport: str = "tcp",
        websocket_path: str | None = None,
        keepalive: int = 60,
        qos: int = 1,
        timeout: float = 10.0,
        tls_enabled: bool = False,
        tls_ca_path: str | None = None,
        tls_cert_path: str | None = None,
        tls_key_path: str | None = None,
        tls_insecure: bool = False,
    ) -> None:
        self._node_id = node_id
        self._hostname = hostname
        self._port = port
        self._username = username
        self._password = password
        self._transport = transport
        self._websocket_path = websocket_path
        self._keepalive = keepalive
        self._qos = qos
        self._timeout = timeout
        self._tls_enabled = tls_enabled
        self._tls_ca_path = tls_ca_path
        self._tls_cert_path = tls_cert_path
        self._tls_key_path = tls_key_path
        self._tls_insecure = tls_insecure

        self._client: Any | None = None
        self._message_task: asyncio.Task[None] | None = None
        self._connected = False
        self._subscriptions: dict[str, list[SubscriptionCallback]] = {}
        self._pending_rpc: dict[str, asyncio.Future[MeshMessage]] = {}
        self._response_topic = f"nexus/rpc/{node_id}/+/response"

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return

        aiomqtt = _load_aiomqtt()
        tls_context = _build_tls_context(
            enabled=self._tls_enabled,
            ca_path=self._tls_ca_path,
            cert_path=self._tls_cert_path,
            key_path=self._tls_key_path,
            insecure=self._tls_insecure,
        )

        client = aiomqtt.Client(
            hostname=self._hostname,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=self._node_id,
            transport=self._transport,
            timeout=self._timeout,
            keepalive=self._keepalive,
            tls_context=tls_context,
            tls_insecure=self._tls_insecure if self._tls_enabled else None,
            websocket_path=self._websocket_path if self._transport == "websockets" else None,
        )
        try:
            await client.__aenter__()
            self._client = client
            self._connected = True
            await self._client.subscribe(self._response_topic, qos=self._qos)
            for topic_pattern in self._subscriptions:
                await self._client.subscribe(topic_pattern, qos=self._qos)
            self._message_task = asyncio.create_task(
                self._message_loop(),
                name=f"mesh-mqtt-{self._node_id}",
            )
        except Exception:
            with suppress(Exception):
                await client.__aexit__(None, None, None)
            self._client = None
            self._connected = False
            raise

    async def disconnect(self) -> None:
        if not self._connected and self._client is None:
            return

        if self._message_task is not None:
            self._message_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._message_task
            self._message_task = None

        for future in self._pending_rpc.values():
            if not future.done():
                future.cancel()
        self._pending_rpc.clear()

        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
        self._connected = False

    async def publish(self, topic: str, message: MeshMessage) -> None:
        if not self._connected or self._client is None:
            raise RuntimeError(f"Transport not connected: {self._node_id}")
        await self._client.publish(topic, message.to_json(), qos=self._qos, timeout=self._timeout)

    async def subscribe(
        self,
        topic_pattern: str,
        callback: SubscriptionCallback,
    ) -> None:
        if not self._connected or self._client is None:
            raise RuntimeError(f"Transport not connected: {self._node_id}")
        callbacks = self._subscriptions.setdefault(topic_pattern, [])
        callbacks.append(callback)
        await self._client.subscribe(topic_pattern, qos=self._qos)

    async def unsubscribe(self, topic_pattern: str) -> None:
        self._subscriptions.pop(topic_pattern, None)
        if not self._connected or self._client is None:
            return
        await self._client.unsubscribe(topic_pattern, timeout=self._timeout)

    async def request(
        self,
        target_node: str,
        payload: dict[str, Any],
        *,
        timeout: float = 30.0,
        source_node: str = "",
    ) -> MeshMessage:
        if not self._connected or self._client is None:
            raise RuntimeError(f"Transport not connected: {self._node_id}")

        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MeshMessage] = loop.create_future()
        self._pending_rpc[request_id] = future

        message = MeshMessage(
            message_id=uuid.uuid4().hex[:16],
            message_type=MessageType.RPC_REQUEST,
            source_node=source_node or self._node_id,
            target_node=target_node,
            topic=f"nexus/rpc/{target_node}/{request_id}",
            payload={**payload, "request_id": request_id},
        )
        await self.publish(message.topic, message)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_rpc.pop(request_id, None)
            raise TimeoutError(
                f"RPC request to {target_node} timed out after {timeout}s"
            ) from None

    async def _message_loop(self) -> None:
        assert self._client is not None
        try:
            async for raw_message in self._client.messages:
                topic = str(raw_message.topic)
                try:
                    message = MeshMessage.from_json(_normalize_payload(raw_message.payload))
                except Exception:
                    logger.warning("Invalid MQTT mesh payload on topic=%s", topic, exc_info=True)
                    continue
                await self._dispatch_message(topic, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._connected:
                logger.warning("MQTT message loop stopped unexpectedly: node=%s", self._node_id, exc_info=True)
        finally:
            self._connected = False

    async def _dispatch_message(self, topic: str, message: MeshMessage) -> None:
        if message.target_node and message.target_node not in {"", self._node_id}:
            return

        for pattern, callbacks in list(self._subscriptions.items()):
            if not _topic_matches(pattern, topic):
                continue
            if message.source_node == self._node_id and not pattern.startswith("nexus/broadcast"):
                continue
            for callback in list(callbacks):
                try:
                    await callback(topic, message)
                except Exception:
                    logger.warning(
                        "Subscription callback failed: topic=%s subscriber=%s",
                        topic,
                        self._node_id,
                        exc_info=True,
                    )

        if message.message_type == MessageType.RPC_RESPONSE:
            request_id = str(message.payload.get("request_id") or "")
            future = self._pending_rpc.pop(request_id, None)
            if future and not future.done():
                future.set_result(message)
