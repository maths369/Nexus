"""Session title generator 测试"""

from __future__ import annotations

from nexus.channel.session_title import _extract_conversation_tail


def test_extract_tail_basic():
    events = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "帮我分析 Vault 索引"},
        {"role": "assistant", "content": "好的，正在分析..."},
    ]
    text = _extract_conversation_tail(events)
    assert "帮我分析" in text
    assert "正在分析" in text
    # system 消息不应出现
    assert "你是助手" not in text


def test_extract_tail_truncates_long_messages():
    events = [
        {"role": "user", "content": "x" * 2000},
        {"role": "assistant", "content": "回复"},
    ]
    text = _extract_conversation_tail(events, max_chars=600)
    # 单条消息截断到 500 字符
    assert len(text) < 1000


def test_extract_tail_respects_max_chars():
    events = [
        {"role": "user", "content": f"消息 {i}" * 50}
        for i in range(20)
    ]
    text = _extract_conversation_tail(events, max_chars=500)
    assert len(text) < 2000  # 应远小于全量


def test_extract_tail_empty_events():
    text = _extract_conversation_tail([])
    assert text == ""
