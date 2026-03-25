from __future__ import annotations

import asyncio

from nexus.channel.adapter_feishu import FeishuAdapter, FeishuLongConnectionRunner


class _FakeResponse:
    def __init__(self, payload=None, *, content: bytes = b"", headers: dict | None = None, status_code: int = 200):
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self):
        self.calls = []

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/auth/v3/tenant_access_token/internal'):
            return _FakeResponse({'code': 0, 'tenant_access_token': 'token-1', 'expire': 7200})
        return _FakeResponse({'code': 0, 'data': {'message_id': 'om_1'}})

    async def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if "/images/" in url:
            return _FakeResponse(
                content=b"image-bytes",
                headers={
                    "content-type": "image/png",
                    "content-disposition": 'attachment; filename="screen.png"',
                },
            )
        return _FakeResponse(
            content=b"file-bytes",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="report.pdf"',
            },
        )

    async def aclose(self):
        return None


def test_feishu_adapter_refreshes_token_and_sends_text():
    client = _FakeHttpClient()
    adapter = FeishuAdapter(
        {
            'app_id': 'app-id',
            'app_secret': 'app-secret',
        },
        client=client,
    )

    asyncio.run(adapter.send_text('oc_xxx', 'hello'))

    assert len(client.calls) == 2
    assert client.calls[0][0].endswith('/auth/v3/tenant_access_token/internal')
    assert client.calls[1][0].endswith('/im/v1/messages')
    assert client.calls[1][1]['json']['receive_id'] == 'oc_xxx'


def test_feishu_adapter_parses_webhook_text_message():
    adapter = FeishuAdapter({"app_id": "app-id", "app_secret": "secret"})
    payload = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_123"}, "sender_type": "user"},
            "message": {
                "message_id": "om_123",
                "chat_id": "oc_123",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text":"你好，Nexus"}',
            },
        },
    }
    parsed = adapter.parse_message_event(payload)
    assert parsed is not None
    assert parsed["ignored"] is False
    assert parsed["message_id"] == "om_123"
    assert parsed["chat_id"] == "oc_123"
    assert parsed["sender_user_id"] == "ou_123"
    assert parsed["text"] == "你好，Nexus"


def test_feishu_adapter_parses_long_connection_message_shape():
    adapter = FeishuAdapter({"app_id": "app-id", "app_secret": "secret"})
    payload = {
        "event_id": "evt-2",
        "message": {
            "message_id": "om_456",
            "chat_id": "oc_456",
            "chat_type": "p2p",
            "message_type": "text",
            "content": '{"text":"长连接测试"}',
        },
        "sender": {"sender_id": {"open_id": "ou_456"}, "sender_type": "user"},
    }
    parsed = adapter.parse_long_connection_message_event(payload)
    assert parsed is not None
    assert parsed["ignored"] is False
    assert parsed["message_id"] == "om_456"
    assert parsed["chat_id"] == "oc_456"
    assert parsed["text"] == "长连接测试"


def test_feishu_adapter_parses_image_message_into_attachment():
    adapter = FeishuAdapter({"app_id": "app-id", "app_secret": "secret"})
    payload = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt-img"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_img"}, "sender_type": "user"},
            "message": {
                "message_id": "om_img",
                "chat_id": "oc_img",
                "chat_type": "p2p",
                "message_type": "image",
                "content": '{"image_key":"img_key_1","file_name":"capture.png"}',
            },
        },
    }
    parsed = adapter.parse_message_event(payload)
    assert parsed is not None
    assert parsed["ignored"] is False
    assert parsed["message_type"] == "image"
    assert parsed["attachments"] == [
        {
            "attachment_type": "image",
            "message_id": "om_img",
            "image_key": "img_key_1",
            "file_name": "capture.png",
            "resource_type": "image",
        }
    ]


def test_feishu_adapter_parses_file_message_into_attachment():
    adapter = FeishuAdapter({"app_id": "app-id", "app_secret": "secret"})
    payload = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt-file"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_file"}, "sender_type": "user"},
            "message": {
                "message_id": "om_file",
                "chat_id": "oc_file",
                "chat_type": "p2p",
                "message_type": "file",
                "content": '{"file_key":"file_key_1","file_name":"report.pdf"}',
            },
        },
    }
    parsed = adapter.parse_message_event(payload)
    assert parsed is not None
    assert parsed["ignored"] is False
    assert parsed["attachments"] == [
        {
            "attachment_type": "file",
            "message_id": "om_file",
            "file_key": "file_key_1",
            "file_name": "report.pdf",
            "resource_type": "file",
        }
    ]


def test_feishu_adapter_downloads_image_attachment():
    client = _FakeHttpClient()
    adapter = FeishuAdapter(
        {"app_id": "app-id", "app_secret": "secret"},
        client=client,
    )

    payload = asyncio.run(
        adapter.download_attachment(
            message_id="om_img",
            attachment={
                "attachment_type": "image",
                "image_key": "img_key_1",
                "file_name": "capture.png",
            },
        )
    )

    assert payload["bytes"] == b"image-bytes"
    assert payload["mime_type"] == "image/png"
    assert payload["file_name"] == "screen.png"


def test_feishu_adapter_downloads_file_attachment():
    client = _FakeHttpClient()
    adapter = FeishuAdapter(
        {"app_id": "app-id", "app_secret": "secret"},
        client=client,
    )

    payload = asyncio.run(
        adapter.download_attachment(
            message_id="om_file",
            attachment={
                "attachment_type": "file",
                "file_key": "file_key_1",
                "file_name": "report.pdf",
                "resource_type": "file",
            },
        )
    )

    assert payload["bytes"] == b"file-bytes"
    assert payload["mime_type"] == "application/pdf"
    assert payload["file_name"] == "report.pdf"


def test_feishu_long_connection_runner_dispatches_parsed_message():
    adapter = FeishuAdapter(
        {
            "app_id": "app-id",
            "app_secret": "secret",
            "subscription_mode": "long_connection",
        }
    )
    received = []

    async def on_message(event):
        received.append(event)

    async def main():
        runner = FeishuLongConnectionRunner(adapter, on_message=on_message, ack_timeout_seconds=0.2)
        runner._loop = asyncio.get_running_loop()  # noqa: SLF001
        runner._on_message_event(  # noqa: SLF001
            {
                "event_id": "evt-3",
                "message": {
                    "message_id": "om_789",
                    "chat_id": "oc_789",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": '{"text":"runner ok"}',
                },
                "sender": {"sender_id": {"open_id": "ou_789"}, "sender_type": "user"},
            }
        )
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert received
    assert received[0]["message_id"] == "om_789"
    assert received[0]["text"] == "runner ok"
