"""
端到端冒烟测试 — 验证完整消息链路无断裂

InboundMessage → Orchestrator → RunManager → reply_fn
"""

from __future__ import annotations

import asyncio
import importlib

from fastapi.testclient import TestClient

from nexus.api.runtime import build_runtime as build_real_runtime
from nexus.agent.types import Run, RunStatus
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.message_formatter import MessageFormatter
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore
from nexus.channel.types import ChannelType, InboundMessage, OutboundMessage
from nexus.orchestrator import Orchestrator
from nexus.shared import NexusSettings


# ---------------------------------------------------------------------------
# Fake 依赖
# ---------------------------------------------------------------------------

class FakeProvider:
    """假 Provider, 用于 SessionRouter 的意图分类"""

    async def chat(self, *, messages, model=None, **kwargs):
        # 返回 NEW_TASK 意图
        class FakeChoice:
            class FakeMessage:
                content = '{"intent": "new_task"}'
                tool_calls = None
            message = FakeMessage()
        class FakeResp:
            choices = [FakeChoice()]
        return FakeResp()


class FakeRunManager:
    """假 RunManager, 返回成功结果"""

    def __init__(self):
        self.last_kwargs = None

    async def execute(self, **kwargs):
        self.last_kwargs = kwargs
        return Run(
            run_id="smoke-run-1",
            session_id=kwargs["session_id"],
            status=RunStatus.SUCCEEDED,
            task=kwargs["task"],
            result="这是冒烟测试的执行结果。任务已完成。",
            model="test-model",
        )


# ---------------------------------------------------------------------------
# FastAPI 入口 Smoke Helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path, *, feishu_enabled: bool = False) -> NexusSettings:
    root = tmp_path
    return NexusSettings(
        root_dir=root,
        config_path=root / "config" / "app.yaml",
        raw={
            "server": {"host": "127.0.0.1", "port": 8000},
            "storage": {
                "sqlite_dir": "./data/sqlite",
                "skills_dir": "./skills",
                "staging_dir": "./data/staging",
                "backups_dir": "./data/backups",
            },
            "provider": {
                "primary": {
                    "name": "kimi",
                    "model": "kimi-k2-0711-preview",
                    "provider_type": "moonshot",
                    "base_url": "https://api.moonshot.cn/v1",
                    "api_key_env": "MOONSHOT_API_KEY",
                }
            },
            "vault": {"base_path": "./vault"},
            "audio": {
                "backend": "sensevoice",
                "language": "zh",
                "sensevoice_model_dir": "./models/sensevoice/SenseVoiceSmall",
                "sensevoice_device": "cpu",
            },
            "tool_policy": {
                "enabled": True,
                "allowlist": [
                    "read_vault",
                    "search_vault",
                    "memory_search",
                    "memory_write",
                    "knowledge_ingest",
                    "create_note",
                    "write_vault",
                    "list_local_files",
                    "code_read_file",
                ],
            },
            "feishu": {
                "enabled": feishu_enabled,
                "verify_signature": False,
                "subscription_mode": "webhook",
            },
            "scheduler": {
                "enabled": False,
                "config_path": "./config/scheduler.yaml",
            },
            "browser": {
                "enabled": False,
                "worker_command": [],
            },
        },
    )


def _patch_test_app(monkeypatch, tmp_path, *, feishu_enabled: bool = False):
    settings = _make_settings(tmp_path, feishu_enabled=feishu_enabled)
    runtime = build_real_runtime(settings=settings)

    async def fake_execute(*, session_id: str, task: str, **kwargs):
        return Run(
            run_id="smoke-run-http",
            session_id=session_id,
            status=RunStatus.SUCCEEDED,
            task=task,
            result=f"已完成：{task}",
            model="test-model",
        )

    runtime.run_manager.execute = fake_execute

    app_module = importlib.import_module("nexus.api.app")
    monkeypatch.setattr(app_module, "load_nexus_settings", lambda: settings)
    monkeypatch.setattr(app_module, "build_runtime", lambda settings=None: runtime)

    sent_messages: list[tuple[str, str]] = []

    class FakeFeishuAdapter:
        def __init__(self, config):
            self.configured = True

        async def send_text(self, chat_id: str, text: str) -> None:
            sent_messages.append((chat_id, text))

        async def aclose(self) -> None:
            return None

    if feishu_enabled:
        monkeypatch.setenv("FEISHU_APP_ID", "test-app")
        monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")
        monkeypatch.setattr(app_module, "FeishuAdapter", FakeFeishuAdapter)

    return app_module, sent_messages


def _patch_attachment_app(monkeypatch, tmp_path):
    settings = _make_settings(tmp_path, feishu_enabled=True)
    runtime = build_real_runtime(settings=settings)

    async def fake_execute(*, session_id: str, task: str, **kwargs):
        return Run(
            run_id="smoke-run-attachment",
            session_id=session_id,
            status=RunStatus.SUCCEEDED,
            task=task,
            result=task,
            model="test-model",
        )

    runtime.run_manager.execute = fake_execute

    app_module = importlib.import_module("nexus.api.app")
    monkeypatch.setattr(app_module, "load_nexus_settings", lambda: settings)
    monkeypatch.setattr(app_module, "build_runtime", lambda settings=None: runtime)

    sent_messages: list[tuple[str, str]] = []

    class FakeFeishuAdapter:
        def __init__(self, config):
            self.configured = True

        def verify_callback(self, *, headers, payload, raw_body):
            return True, "ok"

        def is_url_verification(self, payload):
            return False

        def parse_message_event(self, payload):
            return {
                "ignored": False,
                "message_id": "om_attachment",
                "chat_id": "oc_attachment",
                "chat_type": "p2p",
                "sender_user_id": "ou_attachment",
                "text": "",
                "message_type": "file",
                "attachments": [
                    {
                        "attachment_type": "file",
                        "message_id": "om_attachment",
                        "file_key": "file_key_1",
                        "file_name": "notes.txt",
                        "resource_type": "file",
                    }
                ],
            }

        async def download_attachment(self, *, message_id: str, attachment: dict):
            return {
                "bytes": "附件中的关键内容".encode("utf-8"),
                "mime_type": "text/plain",
                "file_name": "notes.txt",
            }

        async def send_text(self, chat_id: str, text: str) -> None:
            sent_messages.append((chat_id, text))

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(app_module, "FeishuAdapter", FakeFeishuAdapter)
    return app_module, sent_messages


def _patch_multi_attachment_app(monkeypatch, tmp_path):
    settings = _make_settings(tmp_path, feishu_enabled=True)
    runtime = build_real_runtime(settings=settings)

    async def fake_execute(*, session_id: str, task: str, **kwargs):
        return Run(
            run_id="smoke-run-multi-attachment",
            session_id=session_id,
            status=RunStatus.SUCCEEDED,
            task=task,
            result=task,
            model="test-model",
        )

    runtime.run_manager.execute = fake_execute

    app_module = importlib.import_module("nexus.api.app")
    monkeypatch.setattr(app_module, "load_nexus_settings", lambda: settings)
    monkeypatch.setattr(app_module, "build_runtime", lambda settings=None: runtime)

    sent_messages: list[tuple[str, str]] = []

    class FakeFeishuAdapter:
        def __init__(self, config):
            self.configured = True

        def verify_callback(self, *, headers, payload, raw_body):
            return True, "ok"

        def is_url_verification(self, payload):
            return False

        def parse_message_event(self, payload):
            return {
                "ignored": False,
                "message_id": "om_multi_attachment",
                "chat_id": "oc_attachment",
                "chat_type": "p2p",
                "sender_user_id": "ou_attachment",
                "text": "",
                "message_type": "file",
                "attachments": [
                    {
                        "attachment_type": "file",
                        "message_id": "om_multi_attachment",
                        "file_key": "file_key_1",
                        "file_name": "notes.txt",
                        "resource_type": "file",
                    },
                    {
                        "attachment_type": "image",
                        "message_id": "om_multi_attachment",
                        "image_key": "img_key_1",
                        "file_name": "capture.png",
                        "resource_type": "image",
                    },
                ],
            }

        async def download_attachment(self, *, message_id: str, attachment: dict):
            if attachment.get("attachment_type") == "image":
                return {
                    "bytes": b"\x89PNG\r\n",
                    "mime_type": "image/png",
                    "file_name": "capture.png",
                }
            return {
                "bytes": "附件中的关键内容".encode("utf-8"),
                "mime_type": "text/plain",
                "file_name": "notes.txt",
            }

        async def send_text(self, chat_id: str, text: str) -> None:
            sent_messages.append((chat_id, text))

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(app_module, "FeishuAdapter", FakeFeishuAdapter)
    return app_module, sent_messages


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_full_message_pipeline_produces_reply(tmp_path):
    """
    验证完整消息链路:
    1. 构造 InboundMessage
    2. Orchestrator 接收
    3. SessionRouter 路由 → 创建 session
    4. RunManager 执行 → 产生结果
    5. 结果通过 reply_fn 发回
    """
    store = SessionStore(tmp_path / "sessions.db")
    context_window = ContextWindowManager(store)

    fake_provider = FakeProvider()
    router = SessionRouter(
        session_store=store,
        context_window=context_window,
        provider=fake_provider,
    )

    fake_run_manager = FakeRunManager()

    orchestrator = Orchestrator(
        session_router=router,
        session_store=store,
        context_window=context_window,
        run_manager=fake_run_manager,
        formatter=MessageFormatter(),
    )

    replies: list[OutboundMessage] = []

    async def reply_fn(msg: OutboundMessage) -> None:
        replies.append(msg)

    inbound = InboundMessage(
        message_id="smoke-msg-1",
        channel=ChannelType.WEB,
        sender_id="test-user",
        content="帮我整理一下今天的会议纪要",
    )

    asyncio.run(orchestrator.handle_message(inbound, reply_fn))

    # 验证
    assert replies, "Should have received at least one reply"
    assert any("完成" in r.content or "结果" in r.content for r in replies), (
        f"Reply should contain result text, got: {[r.content[:100] for r in replies]}"
    )

    # 验证 RunManager 收到了正确的任务
    assert fake_run_manager.last_kwargs is not None
    assert "会议纪要" in fake_run_manager.last_kwargs["task"]


def test_pipeline_status_query_returns_session_info(tmp_path):
    """验证 STATUS_QUERY 路径能返回 session 信息"""
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("test-user", "web", summary="知识库重建任务")
    context_window = ContextWindowManager(store)

    class StatusQueryProvider:
        async def chat(self, *, messages, model=None, **kwargs):
            class Msg:
                content = f'{{"intent": "status_query", "session_id": "{session.session_id}"}}'
                tool_calls = None
            class Choice:
                message = Msg()
            class Resp:
                choices = [Choice()]
            return Resp()

    router = SessionRouter(
        session_store=store,
        context_window=context_window,
        provider=StatusQueryProvider(),
    )

    orchestrator = Orchestrator(
        session_router=router,
        session_store=store,
        context_window=context_window,
        run_manager=FakeRunManager(),
        formatter=MessageFormatter(),
    )

    replies: list[OutboundMessage] = []

    async def reply_fn(msg: OutboundMessage) -> None:
        replies.append(msg)

    inbound = InboundMessage(
        message_id="status-msg-1",
        channel=ChannelType.WEB,
        sender_id="test-user",
        content="上次那个任务怎么样了",
    )

    asyncio.run(orchestrator.handle_message(inbound, reply_fn))

    assert replies
    assert "知识库重建" in replies[0].content


def test_cli_reindex_command_is_importable():
    """验证 CLI 入口可正常导入"""
    from nexus.__main__ import main, cmd_reindex, cmd_serve, cmd_health, cmd_agent_smoke
    assert callable(main)
    assert callable(cmd_reindex)
    assert callable(cmd_serve)
    assert callable(cmd_health)
    assert callable(cmd_agent_smoke)


def test_app_yaml_is_loadable():
    """验证 app.yaml 可正常加载"""
    from pathlib import Path
    import yaml

    config_path = Path(__file__).resolve().parents[2] / "config" / "app.yaml"
    assert config_path.exists(), f"config/app.yaml not found at {config_path}"

    with open(config_path) as f:
        config = yaml.safe_load(f)

    assert "server" in config
    assert "agent" in config
    assert "provider" in config
    assert "knowledge" in config
    assert "vault" in config
    assert config["server"]["port"] == 8000


def test_http_feishu_webhook_pipeline_replies_via_adapter(tmp_path, monkeypatch):
    app_module, sent_messages = _patch_test_app(
        monkeypatch, tmp_path, feishu_enabled=True
    )

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_test_user"}},
            "message": {
                "message_id": "om_test_msg_1",
                "chat_id": "oc_test_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text": "帮我整理一下今天的会议纪要"}',
            },
        },
    }

    with TestClient(app_module.app) as client:
        response = client.post("/feishu/webhook", json=payload)

    assert response.status_code == 200
    assert response.json() == {"code": 0}
    assert len(sent_messages) == 2
    assert sent_messages[0][0] == "oc_test_chat"
    assert "收到，正在处理" in sent_messages[0][1]
    assert "已完成：帮我整理一下今天的会议纪要" in sent_messages[1][1]


def test_http_feishu_webhook_pipeline_ingests_attachment_and_replies(tmp_path, monkeypatch):
    app_module, sent_messages = _patch_attachment_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        response = client.post("/feishu/webhook", json={"event": {"message": {}}})

    assert response.status_code == 200
    assert response.json() == {"code": 0}
    assert len(sent_messages) == 2
    assert "收到，正在处理" in sent_messages[0][1]
    assert "附加资产摘要" in sent_messages[1][1]
    assert "notes.txt" in sent_messages[1][1]


def test_http_feishu_webhook_pipeline_generates_batch_manifest_for_multi_attachment(tmp_path, monkeypatch):
    app_module, sent_messages = _patch_multi_attachment_app(monkeypatch, tmp_path)

    with TestClient(app_module.app) as client:
        response = client.post("/feishu/webhook", json={"event": {"message": {}}})

    assert response.status_code == 200
    assert response.json() == {"code": 0}
    assert len(sent_messages) == 2
    assert "批量导入清单" in sent_messages[1][1]
    assert "capture.png" in sent_messages[1][1]


def test_websocket_pipeline_produces_ack_and_result(tmp_path, monkeypatch):
    app_module, _ = _patch_test_app(monkeypatch, tmp_path, feishu_enabled=False)

    with TestClient(app_module.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}

            websocket.send_json(
                {
                    "type": "message",
                    "seq": 1,
                    "sender_id": "web-test-user",
                    "content": "帮我生成飞书 API 传输方案",
                }
            )

            first = websocket.receive_json()
            second = websocket.receive_json()

    assert {first["type"], second["type"]} == {"ack", "result"}
    assert first["session_id"] == second["session_id"]
    contents = [first["content"], second["content"]]
    assert any("收到，正在处理" in item for item in contents)
    assert any("已完成：帮我生成飞书 API 传输方案" in item for item in contents)
