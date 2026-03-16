"""SessionStore 测试 — CRUD、事件、搜索"""

from __future__ import annotations

from nexus.channel.session_store import SessionStore, SessionStatus


def test_create_and_get_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "feishu", summary="测试任务")

    assert session.sender_id == "user-1"
    assert session.channel == "feishu"
    assert session.status == SessionStatus.ACTIVE
    assert session.summary == "测试任务"

    # get by id
    fetched = store.get_session(session.session_id)
    assert fetched is not None
    assert fetched.session_id == session.session_id


def test_get_active_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    s1 = store.create_session("user-1", "feishu", summary="旧任务")
    store.update_session_status(s1.session_id, SessionStatus.COMPLETED)
    s2 = store.create_session("user-1", "feishu", summary="新任务")

    active = store.get_active_session("user-1")
    assert active is not None
    assert active.session_id == s2.session_id


def test_update_session_status(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "web")
    store.update_session_status(session.session_id, SessionStatus.PAUSED)

    fetched = store.get_session(session.session_id)
    assert fetched.status == SessionStatus.PAUSED


def test_update_session_summary(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "web")
    store.update_session_summary(session.session_id, "知识库重建")

    fetched = store.get_session(session.session_id)
    assert fetched.summary == "知识库重建"


def test_update_session_metadata(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "web")
    store.update_session_metadata(session.session_id, {"tool_count": 5})

    fetched = store.get_session(session.session_id)
    assert fetched.metadata["tool_count"] == 5


def test_add_and_get_events(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "web")

    store.add_event(session.session_id, "user", "帮我整理会议纪要")
    store.add_event(session.session_id, "assistant", "好的，正在整理...")
    store.add_event(session.session_id, "assistant", "整理完成，共 5 个议题。")

    events = store.get_events(session.session_id)
    assert len(events) == 3
    assert events[0].role == "user"
    assert events[1].role == "assistant"


def test_get_events_with_limit(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "web")

    for i in range(10):
        store.add_event(session.session_id, "user", f"Message {i}")

    # limit=3 应该返回最近的 3 条（DESC 排序）
    events = store.get_events(session.session_id, limit=3)
    assert len(events) == 3


def test_get_recent_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    for i in range(5):
        store.create_session("user-1", "web", summary=f"任务 {i}")

    recent = store.get_recent_sessions("user-1", limit=3)
    assert len(recent) == 3


def test_find_relevant_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    s1 = store.create_session("user-1", "web", summary="飞书 API 接入方案")
    store.add_event(s1.session_id, "user", "帮我设计飞书事件回调的接口")

    s2 = store.create_session("user-1", "web", summary="知识库索引优化")
    store.add_event(s2.session_id, "user", "重建 FTS5 全文索引")

    s3 = store.create_session("user-1", "web", summary="周报")
    store.add_event(s3.session_id, "user", "帮我写这周的周报")

    # 搜索 "飞书" 应该优先返回 s1
    results = store.find_relevant_sessions(sender_id="user-1", query="飞书", limit=5)
    assert results[0].session_id == s1.session_id

    # 搜索 "索引" 应该优先返回 s2
    results = store.find_relevant_sessions(sender_id="user-1", query="索引", limit=5)
    assert results[0].session_id == s2.session_id


def test_no_active_session_returns_none(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    assert store.get_active_session("unknown-user") is None


def test_get_most_recent_session(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    s1 = store.create_session("user-1", "web", summary="First")
    store.update_session_status(s1.session_id, SessionStatus.COMPLETED)
    s2 = store.create_session("user-1", "web", summary="Second")
    store.update_session_status(s2.session_id, SessionStatus.PAUSED)

    recent = store.get_most_recent_session("user-1")
    assert recent is not None
    assert recent.session_id == s2.session_id


def test_append_and_get_recent_artifacts(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "feishu", summary="附件测试")

    store.append_recent_artifacts(
        session.session_id,
        [
            {
                "artifact_id": "art_1",
                "artifact_type": "file",
                "filename": "report.pdf",
                "relative_path": "_system/artifacts/files/2026/03/report.pdf",
                "page_relative_path": "inbox/imports/feishu/导入-PDF-report.md",
            }
        ],
    )

    artifacts = store.get_recent_artifacts(session.session_id)
    assert len(artifacts) == 1
    assert artifacts[0]["filename"] == "report.pdf"
    assert artifacts[0]["page_relative_path"] == "inbox/imports/feishu/导入-PDF-report.md"
