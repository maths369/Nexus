from __future__ import annotations

from datetime import datetime, timedelta

from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore, SessionStatus
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


def test_router_follows_up_completed_session_within_freshness(tmp_path):
    """Run 成功后 session 保持 ACTIVE，但即使被标记为 COMPLETED，
    只要在 freshness 窗口内也应延续为 FOLLOW_UP（多轮对话核心保障）。"""
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("u1", "feishu", summary="讨论架构设计")
    store.add_event(session.session_id, "user", "帮我分析这个架构")
    store.add_event(session.session_id, "assistant", "好的，这个架构有以下特点...")
    # 模拟 run 完成后 session 仍为 active（新逻辑），或旧逻辑下为 completed
    store.update_session_status(session.session_id, SessionStatus.COMPLETED)

    router = SessionRouter(store, ContextWindowManager(store, freshness_minutes=60))
    decision = __import__("asyncio").run(router.route(_message("那性能方面呢？")))

    assert decision.intent == MessageIntent.FOLLOW_UP
    assert decision.session_id == session.session_id


def test_router_channel_aware_session_lookup(tmp_path):
    """同一用户在不同通道的 session 应独立，飞书消息优先匹配飞书 session。"""
    store = SessionStore(tmp_path / "sessions.db")
    s_web = store.create_session("u1", "web", summary="Web 对话")
    store.add_event(s_web.session_id, "user", "web 上的问题")
    s_feishu = store.create_session("u1", "feishu", summary="飞书对话")
    store.add_event(s_feishu.session_id, "user", "飞书上的问题")

    router = SessionRouter(store, ContextWindowManager(store, freshness_minutes=60))
    msg = InboundMessage(
        message_id="m1",
        channel=ChannelType.FEISHU,
        sender_id="u1",
        content="继续讨论",
        metadata={"channel_key": "feishu"},
    )
    decision = __import__("asyncio").run(router.route(msg))

    assert decision.intent == MessageIntent.FOLLOW_UP
    assert decision.session_id == s_feishu.session_id


def test_router_new_task_on_freshness_timeout(tmp_path):
    """超过 freshness 窗口后，应创建新任务。"""
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("u1", "feishu", summary="旧对话")
    store.add_event(session.session_id, "user", "旧消息")

    router = SessionRouter(store, ContextWindowManager(store, freshness_minutes=5))
    future_ts = datetime.now() + timedelta(minutes=10)
    msg = InboundMessage(
        message_id="m1",
        channel=ChannelType.FEISHU,
        sender_id="u1",
        content="全新的话题",
        timestamp=future_ts,
    )
    decision = __import__("asyncio").run(router.route(msg))

    assert decision.intent == MessageIntent.NEW_TASK


def test_router_recognizes_slash_new_chinese(tmp_path):
    """/新对话、/新任务 等中文斜杠命令应识别为 COMMAND:new。"""
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))

    for cmd_text in ["/新对话", "/新任务", "/new", "新对话", "新任务"]:
        decision = __import__("asyncio").run(router.route(_message(cmd_text)))
        assert decision.intent == MessageIntent.COMMAND, f"Failed for: {cmd_text}"
        assert decision.metadata["action"] == "new", f"Failed for: {cmd_text}"


def test_router_recognizes_slash_chinese_commands(tmp_path):
    """/暂停、/继续、/状态 等中文斜杠命令。"""
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))

    cases = [
        ("/暂停", "pause"),
        ("/继续", "resume"),
        ("/状态", "status"),
        ("/压缩", "compress"),
        ("/帮助", "help"),
    ]
    for cmd_text, expected_action in cases:
        decision = __import__("asyncio").run(router.route(_message(cmd_text)))
        assert decision.intent == MessageIntent.COMMAND, f"Failed for: {cmd_text}"
        assert decision.metadata["action"] == expected_action, f"Failed for: {cmd_text}"


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
