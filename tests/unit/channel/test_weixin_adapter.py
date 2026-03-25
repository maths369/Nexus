from __future__ import annotations

import asyncio
from pathlib import Path

from nexus.channel.adapter_weixin import WeixinAdapter, WeixinLongPollRunner


class _FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []
        self._host_accounts = {
            "acct-1": {
                "token": "bot-token-1",
                "baseUrl": "https://ilinkai.weixin.qq.com",
                "userId": "owner@im.wechat",
            }
        }

    async def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        if url.endswith("/login/start"):
            return _FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "session_key": "session-1",
                        "account_id": "acct-1",
                        "qrcode_url": "https://example.com/qr.png",
                    },
                }
            )
        if url.endswith("/login/wait"):
            return _FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "connected": True,
                        "account_id": "acct-1",
                        "user_id": "owner@im.wechat",
                    },
                }
            )
        if url.endswith("/messages/send_text"):
            return _FakeResponse({"code": 0, "data": {"message_id": "msg-1"}})
        if url.endswith("/updates/poll"):
            return _FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "ret": 0,
                        "events": [
                            {
                                "ignored": False,
                                "account_id": "acct-1",
                                "message_id": "123",
                                "sender_user_id": "peer@im.wechat",
                                "text": "你好，微信 Nexus",
                                "context_token": "ctx-1",
                                "message_type": "text",
                            }
                        ],
                    },
                }
            )
        raise AssertionError(f"unexpected POST {url}")

    async def aclose(self):
        return None


def _seed_plugin_account(tmp_path: Path, account_id: str = "acct-1") -> None:
    accounts_dir = tmp_path / "plugin-host" / "openclaw-weixin" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / f"{account_id}.json").write_text(
        '{"token":"bot-token-1","baseUrl":"https://ilinkai.weixin.qq.com","userId":"owner@im.wechat"}',
        encoding="utf-8",
    )


def test_weixin_adapter_start_wait_login_and_send_text(tmp_path: Path):
    _seed_plugin_account(tmp_path)
    client = _FakeHttpClient()
    adapter = WeixinAdapter(
        {
            "enabled": True,
            "state_dir": str(tmp_path),
            "plugin_state_dir": str(tmp_path / "plugin-host"),
            "plugin_host_base_url": "http://127.0.0.1:18101",
        },
        client=client,
    )

    started = asyncio.run(adapter.start_login(account_id="acct-1"))
    assert started["account_id"] == "acct-1"
    assert started["qrcode_url"] == "https://example.com/qr.png"

    waited = asyncio.run(adapter.wait_for_login(started["session_key"], timeout_ms=2000))
    assert waited["connected"] is True
    assert waited["account_id"] == "acct-1"

    account = adapter.load_account("acct-1")
    assert account is not None
    assert account.token == "bot-token-1"
    assert account.user_id == "owner@im.wechat"

    asyncio.run(adapter.send_text("acct-1", "peer@im.wechat", "hello", context_token="ctx-1"))
    send_url, send_kwargs = client.post_calls[-1]
    assert send_url.endswith("/messages/send_text")
    assert send_kwargs["json"]["to_user_id"] == "peer@im.wechat"
    assert send_kwargs["json"]["context_token"] == "ctx-1"


def test_weixin_adapter_parses_host_event_and_persists_context_token(tmp_path: Path):
    adapter = WeixinAdapter(
        {
            "enabled": True,
            "state_dir": str(tmp_path),
            "plugin_state_dir": str(tmp_path / "plugin-host"),
        }
    )

    event = adapter.parse_update_message(
        "acct-1",
        {
            "ignored": False,
            "account_id": "acct-1",
            "message_id": "99",
            "sender_user_id": "peer@im.wechat",
            "text": "一条测试消息",
            "context_token": "ctx-99",
        },
    )

    assert event is not None
    assert event["ignored"] is False
    assert event["message_id"] == "99"
    assert event["sender_user_id"] == "peer@im.wechat"
    assert event["text"] == "一条测试消息"
    assert adapter.get_context_token("acct-1", "peer@im.wechat") == "ctx-99"


def test_weixin_long_poll_runner_dispatches_message(tmp_path: Path):
    _seed_plugin_account(tmp_path)
    client = _FakeHttpClient()
    adapter = WeixinAdapter(
        {
            "enabled": True,
            "state_dir": str(tmp_path),
            "plugin_state_dir": str(tmp_path / "plugin-host"),
        },
        client=client,
    )
    received = []

    async def on_message(event):
        received.append(event)

    async def main():
        runner = WeixinLongPollRunner(
            adapter,
            on_message=on_message,
            long_poll_timeout_ms=1000,
            retry_delay_seconds=0.01,
            backoff_delay_seconds=0.02,
        )
        runner.start(loop=asyncio.get_running_loop())
        runner.ensure_account("acct-1")
        await asyncio.sleep(0.05)
        runner.shutdown()
        await asyncio.sleep(0.01)

    asyncio.run(main())

    assert received
    assert received[0]["message_id"] == "123"
    assert adapter.get_context_token("acct-1", "peer@im.wechat") == "ctx-1"
