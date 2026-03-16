"""TodoManager 测试 — 进度追踪、验证、提醒"""

from __future__ import annotations

import pytest
from nexus.agent.todo import TodoManager


def test_update_and_render():
    """基本更新和渲染"""
    tm = TodoManager()
    result = tm.update([
        {"id": "1", "content": "搭建项目框架", "status": "completed"},
        {"id": "2", "content": "编写测试", "status": "in_progress"},
        {"id": "3", "content": "部署上线", "status": "pending"},
    ])
    assert "[x]" in result
    assert "[>]" in result
    assert "[ ]" in result
    assert "1/3 已完成" in result


def test_update_replaces_all():
    """update 是全量替换"""
    tm = TodoManager()
    tm.update([
        {"content": "task A", "status": "pending"},
        {"content": "task B", "status": "pending"},
    ])
    assert len(tm.items) == 2

    tm.update([{"content": "task C", "status": "in_progress"}])
    assert len(tm.items) == 1
    assert tm.items[0]["content"] == "task C"


def test_empty_items():
    """空列表也可以"""
    tm = TodoManager()
    result = tm.update([])
    assert result == "无任务。"
    assert tm.items == []


def test_max_items_exceeded():
    """超过 20 项报错"""
    tm = TodoManager()
    items = [{"content": f"task {i}", "status": "pending"} for i in range(21)]
    with pytest.raises(ValueError, match="最多"):
        tm.update(items)


def test_multiple_in_progress_rejected():
    """同一时间只能有一个 in_progress"""
    tm = TodoManager()
    with pytest.raises(ValueError, match="in_progress"):
        tm.update([
            {"content": "A", "status": "in_progress"},
            {"content": "B", "status": "in_progress"},
        ])


def test_invalid_status_rejected():
    """无效状态报错"""
    tm = TodoManager()
    with pytest.raises(ValueError, match="无效状态"):
        tm.update([{"content": "A", "status": "done"}])


def test_empty_content_rejected():
    """空内容报错"""
    tm = TodoManager()
    with pytest.raises(ValueError, match="content 必填"):
        tm.update([{"content": "", "status": "pending"}])


def test_nag_not_triggered_initially():
    """初始状态不应触发提醒"""
    tm = TodoManager(nag_after=3)
    assert not tm.should_nag


def test_nag_triggered_after_rounds():
    """连续若干轮未更新后触发提醒"""
    tm = TodoManager(nag_after=2)
    tm.update([{"content": "写代码", "status": "in_progress"}])
    assert not tm.should_nag

    tm.tick()
    assert not tm.should_nag

    tm.tick()
    assert tm.should_nag

    # 更新后重置
    tm.update([{"content": "写代码", "status": "completed"}])
    assert not tm.should_nag


def test_nag_not_triggered_if_all_completed():
    """全部完成后不触发提醒"""
    tm = TodoManager(nag_after=1)
    tm.update([{"content": "A", "status": "completed"}])
    tm.tick()
    tm.tick()
    assert not tm.should_nag


def test_nag_message_with_active_item():
    """提醒消息包含当前进行中的任务"""
    tm = TodoManager()
    tm.update([
        {"content": "写测试", "status": "in_progress", "activeForm": "正在编写测试"},
    ])
    msg = tm.get_nag_message()
    assert "正在编写测试" in msg
    assert "<reminder>" in msg


def test_nag_message_without_active():
    """没有进行中任务时的通用提醒"""
    tm = TodoManager()
    tm.update([{"content": "A", "status": "pending"}])
    msg = tm.get_nag_message()
    assert "todo_write" in msg


def test_active_item_property():
    """active_item 返回当前进行中的任务"""
    tm = TodoManager()
    assert tm.active_item is None

    tm.update([
        {"content": "A", "status": "pending"},
        {"content": "B", "status": "in_progress"},
    ])
    assert tm.active_item is not None
    assert tm.active_item["content"] == "B"


def test_reset():
    """reset 清空所有任务"""
    tm = TodoManager()
    tm.update([{"content": "A", "status": "pending"}])
    tm.tick()
    tm.tick()
    tm.reset()
    assert tm.items == []
    assert not tm.should_nag


def test_auto_id_generation():
    """不提供 id 时自动生成"""
    tm = TodoManager()
    tm.update([
        {"content": "A", "status": "pending"},
        {"content": "B", "status": "pending"},
    ])
    assert tm.items[0]["id"] == "1"
    assert tm.items[1]["id"] == "2"


def test_text_field_alias():
    """兼容 text 字段（learn-claude-code 兼容）"""
    tm = TodoManager()
    tm.update([{"text": "legacy task", "status": "pending"}])
    assert tm.items[0]["content"] == "legacy task"
