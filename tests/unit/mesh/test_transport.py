"""Tests for MQTTTransport."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nexus.mesh.transport import MQTTTransport, MeshMessage, MessageType


class _FakeIncomingMessage:
    def __init__(self, topic: str, payload: str) -> None:
        self.topic = topic
        self.payload = payload


class _FakeMessages:
    def __init__(self, queue: asyncio.Queue[_FakeIncomingMessage]) -> None:
        self._queue = queue

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.connected = False
        self.published: list[tuple[str, str, int]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.unsubscribed: list[str] = []
        self._queue: asyncio.Queue[_FakeIncomingMessage] = asyncio.Queue()
        self.messages = _FakeMessages(self._queue)
        type(self).instances.append(self)

    async def __aenter__(self):
        self.connected = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.connected = False

    async def subscribe(self, topic, qos=0, **kwargs):
        self.subscriptions.append((topic, qos))
        return (qos,)

    async def unsubscribe(self, topic, **kwargs):
        self.unsubscribed.append(topic)
        return None

    async def publish(self, topic, payload=None, qos=0, **kwargs):
        self.published.append((topic, payload, qos))
        return None

    def feed(self, topic: str, message: MeshMessage) -> None:
        self._queue.put_nowait(_FakeIncomingMessage(topic, message.to_json()))


def _fake_aiomqtt_module():
    _FakeClient.instances.clear()
    return SimpleNamespace(Client=_FakeClient)


@pytest.mark.asyncio
async def test_mqtt_transport_dispatches_messages_and_rpc(monkeypatch):
    monkeypatch.setattr("nexus.mesh.transport._load_aiomqtt", _fake_aiomqtt_module)

    transport = MQTTTransport(
        "ubuntu-server-5090",
        hostname="localhost",
        port=1883,
        qos=1,
    )
    await transport.connect()

    client = _FakeClient.instances[0]
    assert (f"nexus/rpc/{transport.node_id}/+/response", 1) in client.subscriptions

    received: list[tuple[str, MeshMessage]] = []

    async def on_card(topic: str, message: MeshMessage) -> None:
        received.append((topic, message))

    await transport.subscribe("nexus/nodes/+/card", on_card)

    remote_message = transport.make_message(
        MessageType.NODE_REGISTER,
        "nexus/nodes/macbook-pro/card",
        {
            "node_id": "macbook-pro",
            "node_type": "edge",
            "display_name": "MacBook Pro",
            "platform": "macos",
            "capabilities": [],
        },
        target_node="",
    )
    remote_message.source_node = "macbook-pro"
    client.feed("nexus/nodes/macbook-pro/card", remote_message)
    await asyncio.sleep(0)

    assert len(received) == 1
    assert received[0][1].source_node == "macbook-pro"

    self_message = transport.make_message(
        MessageType.NODE_REGISTER,
        "nexus/nodes/ubuntu-server-5090/card",
        {"node_id": transport.node_id},
    )
    client.feed("nexus/nodes/ubuntu-server-5090/card", self_message)
    await asyncio.sleep(0)
    assert len(received) == 1

    pending = asyncio.create_task(
        transport.request(
            "macbook-pro",
            {"tool_name": "browser_navigate", "arguments": {"url": "https://example.com"}},
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)

    request_topic, request_payload, request_qos = client.published[-1]
    assert request_topic.startswith("nexus/rpc/macbook-pro/")
    assert request_qos == 1

    request_message = MeshMessage.from_json(request_payload)
    request_id = request_message.payload["request_id"]
    response = MeshMessage(
        message_id="response-1",
        message_type=MessageType.RPC_RESPONSE,
        source_node="macbook-pro",
        target_node=transport.node_id,
        topic=f"nexus/rpc/{transport.node_id}/{request_id}/response",
        payload={"request_id": request_id, "ok": True},
    )
    client.feed(response.topic, response)

    result = await pending
    assert result.payload["ok"] is True

    await transport.unsubscribe("nexus/nodes/+/card")
    assert "nexus/nodes/+/card" in client.unsubscribed

    await transport.disconnect()
    assert transport.connected is False
