from __future__ import annotations

from datetime import datetime, timedelta

from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore
from nexus.channel.types import ChannelType, InboundMessage, MessageIntent
from nexus.provider.gateway import ProviderConfig, ProviderGateway


def _message(content: str, *, sender: str = "u1") -> InboundMessage:
    return InboundMessage(
        message_id="m1",
        channel=ChannelType.FEISHU,
        sender_id=sender,
        content=content,
    )


def test_router_follows_up_active_fresh_session(tmp_path):
    """Active/fresh session → FOLLOW_UP (no keyword-based new-task markers)."""
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("u1", "feishu", summary="分析 OpenClaw skills 架构")
    store.add_event(session.session_id, "user", "帮我分析 OpenClaw skills 架构")

    router = SessionRouter(store, ContextWindowManager(store))
    decision = __import__("asyncio").run(router.route(_message("另外，帮我整理今天的会议纪要")))

    assert decision.intent == MessageIntent.FOLLOW_UP
    assert decision.session_id == session.session_id


def test_router_returns_status_query_for_progress_questions(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("u1", "feishu", summary="生成飞书 API 传输方案")
    store.add_event(session.session_id, "user", "帮我生成飞书 API 传输方案")

    router = SessionRouter(store, ContextWindowManager(store))
    decision = __import__("asyncio").run(router.route(_message("为什么还没回复")))

    assert decision.intent == MessageIntent.STATUS_QUERY
    assert decision.session_id == session.session_id


def test_router_clarifies_when_multiple_historical_candidates_exist(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    first = store.create_session("u1", "feishu", summary="OpenClaw 架构分析")
    second = store.create_session("u1", "feishu", summary="OpenClaw skills 管理分析")
    store.add_event(first.session_id, "user", "分析 OpenClaw 架构")
    store.add_event(second.session_id, "user", "分析 OpenClaw skills 管理")
    store.update_session_status(first.session_id, first.status.COMPLETED)
    store.update_session_status(second.session_id, second.status.COMPLETED)

    router = SessionRouter(store, ContextWindowManager(store, freshness_minutes=1))
    # 用未来时间戳确保 session 已过期（超出 freshness 窗口）
    future_ts = datetime.now() + timedelta(minutes=10)
    msg = InboundMessage(
        message_id="m1",
        channel=ChannelType.FEISHU,
        sender_id="u1",
        content="继续刚才那个 OpenClaw 分析",
        timestamp=future_ts,
    )
    decision = __import__("asyncio").run(router.route(msg))

    assert decision.intent == MessageIntent.UNKNOWN
    assert len(decision.metadata["candidates"]) >= 2


def test_router_does_not_scan_historical_sessions_without_explicit_history_marker(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    first = store.create_session("u1", "feishu", summary="我上传给你个PDF文件，你帮我管理在vault中")
    second = store.create_session("u1", "feishu", summary="我希望你给自己安装PDF阅读的能力")
    store.add_event(first.session_id, "user", "我上传给你个PDF文件，你帮我管理在vault中")
    store.add_event(second.session_id, "user", "我希望你给自己安装PDF阅读的能力")
    store.update_session_status(first.session_id, first.status.COMPLETED)
    store.update_session_status(second.session_id, second.status.COMPLETED)

    router = SessionRouter(store, ContextWindowManager(store, freshness_minutes=1))
    # 用未来时间戳确保 session 已过期（超出 freshness 窗口）
    future_ts = datetime.now() + timedelta(minutes=10)
    msg = InboundMessage(
        message_id="m1",
        channel=ChannelType.FEISHU,
        sender_id="u1",
        content="列出现在你已经有的PDF文件",
        timestamp=future_ts,
    )
    decision = __import__("asyncio").run(router.route(msg))

    assert decision.intent == MessageIntent.NEW_TASK
    assert decision.reason == "default new task"


def test_context_window_reset_excludes_old_messages(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("u1", "feishu", summary="测试上下文")
    store.add_event(session.session_id, "user", "第一条")
    store.add_event(session.session_id, "assistant", "第二条")
    manager = ContextWindowManager(store)
    manager.reset(session.session_id)
    store.add_event(session.session_id, "user", "重置后的问题")

    messages = manager.build_context(session.session_id)

    assert len(messages) == 1
    assert messages[0]["content"] == "重置后的问题"


def test_router_treats_attachment_message_as_new_task(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("u1", "feishu", summary="旧任务")
    store.add_event(session.session_id, "user", "帮我安装 excel 相关能力")

    router = SessionRouter(store, ContextWindowManager(store))
    decision = __import__("asyncio").run(
        router.route(
            InboundMessage(
                message_id="m-attach",
                channel=ChannelType.FEISHU,
                sender_id="u1",
                content="[附加资产摘要]\n- file `report.pdf` 已保存到 `vault/_system/...`",
                attachments=[{"artifact_id": "art_1", "filename": "report.pdf"}],
            )
        )
    )

    assert decision.intent == MessageIntent.NEW_TASK
    assert decision.reason == "message contains attachments"


# DELETED: test_router_treats_capability_install_request_as_new_task
# DELETED: test_router_treats_inventory_query_as_new_task
# Reason: _is_capability_install_request() and _is_inventory_query() were removed
# from SessionRouter. These keyword-based detections no longer exist; the router
# now delegates intent understanding to the LLM.


def test_router_dedupes_and_sanitizes_candidates():
    class _Session:
        def __init__(self, session_id: str, summary: str, status: str = "active"):
            from datetime import datetime
            self.session_id = session_id
            self.summary = summary
            self.status = type("Status", (), {"value": status})()
            self.updated_at = datetime.now()

    sessions = [
        _Session("s1", "可选项：1. 我上传给你个PDF文件，你帮我管理在vault中，请确认。"),
        _Session("s2", "1. 我上传给你个PDF文件，你帮我管理在vault中"),
    ]

    candidates = SessionRouter._dedupe_candidates(sessions)
    assert len(candidates) == 1
    assert "可选项" not in candidates[0]["summary"]
    assert candidates[0]["summary"].startswith("我上传给你个PDF文件")


def test_router_recognizes_provider_slash_command(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    provider = ProviderGateway(
        primary=ProviderConfig(name="qwen", model="qwen-plus"),
        fallbacks=[ProviderConfig(name="gemini", model="gemini-2.5-flash")],
    )
    router = SessionRouter(store, ContextWindowManager(store), provider=provider)

    decision = __import__("asyncio").run(router.route(_message("/provider gemini")))

    assert decision.intent == MessageIntent.COMMAND
    assert decision.reason == "command:provider"
    assert decision.metadata["action"] == "provider"
    assert decision.metadata["provider_command"] == "switch"
    assert decision.metadata["target"] == "gemini"


def test_router_recognizes_provider_status_command(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))

    decision = __import__("asyncio").run(router.route(_message("/provider")))

    assert decision.intent == MessageIntent.COMMAND
    assert decision.reason == "command:provider"
    assert decision.metadata["provider_command"] == "status"


def test_router_recognizes_search_slash_command(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))

    decision = __import__("asyncio").run(router.route(_message("/search bing")))

    assert decision.intent == MessageIntent.COMMAND
    assert decision.reason == "command:search"
    assert decision.metadata["action"] == "search_provider"
    assert decision.metadata["search_command"] == "switch"
    assert decision.metadata["target"] == "bing"


def test_router_recognizes_search_status_command(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))

    decision = __import__("asyncio").run(router.route(_message("/search")))

    assert decision.intent == MessageIntent.COMMAND
    assert decision.reason == "command:search"
    assert decision.metadata["search_command"] == "status"
