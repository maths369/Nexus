"""ContextCompressor 测试 — 三层压缩管线"""

from __future__ import annotations

import json
import pytest
import asyncio
from pathlib import Path

from nexus.agent.compressor import ContextCompressor
from nexus.agent.transcript_store import TranscriptStore


def _make_messages(n_tool_calls: int = 5) -> list[dict]:
    """构造包含多轮工具调用的消息列表"""
    messages = [
        {"role": "system", "content": "你是一个助手。"},
        {"role": "user", "content": "帮我分析代码"},
    ]
    for i in range(n_tool_calls):
        call_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "function": {"name": f"tool_{i}", "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"这是工具 {i} 的输出结果，" + "a" * 200,  # 长内容
        })
    messages.append({"role": "assistant", "content": "分析完成。"})
    return messages


# ---------------------------------------------------------------------------
# Layer 1: micro_compact
# ---------------------------------------------------------------------------

def test_micro_compact_clears_old_results():
    """micro_compact 清理旧的长 tool result，保留最近 N 个"""
    compressor = ContextCompressor(keep_recent=2)
    messages = _make_messages(5)

    compressor._micro_compact(messages)

    # 收集所有 tool messages
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 5

    # 最后 2 个保留完整内容
    assert "这是工具 3 的输出结果" in tool_msgs[3]["content"]
    assert "这是工具 4 的输出结果" in tool_msgs[4]["content"]

    # 前 3 个被替换为 placeholder
    assert "[Previous: used tool_0]" == tool_msgs[0]["content"]
    assert "[Previous: used tool_1]" == tool_msgs[1]["content"]
    assert "[Previous: used tool_2]" == tool_msgs[2]["content"]


def test_micro_compact_keeps_all_if_few():
    """工具结果数量 ≤ keep_recent 时不清理"""
    compressor = ContextCompressor(keep_recent=5)
    messages = _make_messages(3)
    original_contents = [
        m["content"] for m in messages if m.get("role") == "tool"
    ]

    compressor._micro_compact(messages)

    current_contents = [
        m["content"] for m in messages if m.get("role") == "tool"
    ]
    assert original_contents == current_contents


def test_micro_compact_short_content_not_cleared():
    """短内容（≤100 字符）不被替换"""
    compressor = ContextCompressor(keep_recent=1)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "t1", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "short"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "function": {"name": "t2", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c2", "content": "latest " + "x" * 200},
    ]

    compressor._micro_compact(messages)

    # 短内容保留
    assert messages[1]["content"] == "short"


def test_micro_compact_stats():
    """统计数据正确更新"""
    compressor = ContextCompressor(keep_recent=1)
    messages = _make_messages(4)
    compressor._micro_compact(messages)
    assert compressor.stats["micro_compact_count"] == 3


# ---------------------------------------------------------------------------
# Layer 2: auto_compact (无 provider 时使用规则摘要)
# ---------------------------------------------------------------------------

def test_auto_compact_rule_based():
    """无 provider 时使用规则摘要"""
    compressor = ContextCompressor(token_threshold=100)
    messages = _make_messages(3)

    result = asyncio.run(compressor._auto_compact(messages))

    # 应压缩为 system + user(summary) + assistant(ack)
    assert len(result) == 3
    assert result[0]["role"] == "system"
    assert result[1]["role"] == "user"
    assert "上下文已压缩" in result[1]["content"]
    assert result[2]["role"] == "assistant"
    assert compressor.stats["auto_compact_count"] == 1


def test_auto_compact_preserves_system_message():
    """auto_compact 保留 system 消息"""
    compressor = ContextCompressor()
    messages = [
        {"role": "system", "content": "我是系统提示"},
        {"role": "user", "content": "测试"},
        {"role": "assistant", "content": "收到"},
    ]

    result = asyncio.run(compressor._auto_compact(messages))
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "我是系统提示"


def test_auto_compact_handles_no_system():
    """没有 system 消息时也能工作"""
    compressor = ContextCompressor()
    messages = [
        {"role": "user", "content": "测试"},
        {"role": "assistant", "content": "收到"},
    ]

    result = asyncio.run(compressor._auto_compact(messages))
    assert len(result) == 2  # user(summary) + assistant(ack)
    assert result[0]["role"] == "user"


# ---------------------------------------------------------------------------
# Transcript 保存
# ---------------------------------------------------------------------------

def test_save_transcript(tmp_path):
    """transcript 保存为 JSONL"""
    compressor = ContextCompressor(transcript_dir=tmp_path)
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]

    path_str = compressor._save_transcript(messages)
    assert path_str is not None

    path = Path(path_str)
    assert path.exists()

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    assert json.loads(lines[0])["kind"] == "snapshot"
    assert json.loads(lines[1])["message"]["role"] == "user"


def test_save_transcript_uses_transcript_store(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")
    compressor = ContextCompressor(transcript_store=store)

    path_str = compressor._save_transcript(
        [{"role": "user", "content": "hello"}],
        session_id="sess-1",
        run_id="run-1",
    )

    assert path_str is not None
    latest = store.load_latest_snapshot("sess-1")
    assert latest == [{"role": "user", "content": "hello"}]


def test_save_transcript_no_dir():
    """没有 transcript_dir 时返回 None"""
    compressor = ContextCompressor(transcript_dir=None)
    result = compressor._save_transcript([{"role": "user", "content": "test"}])
    assert result is None


# ---------------------------------------------------------------------------
# compress_before_call (集成)
# ---------------------------------------------------------------------------

def test_compress_before_call_micro_only():
    """低于阈值时只执行 micro_compact"""
    compressor = ContextCompressor(
        token_threshold=999_999,  # 超高阈值
        keep_recent=1,
    )
    messages = _make_messages(3)
    original_len = len(messages)

    result = asyncio.run(compressor.compress_before_call(messages))

    # 消息数量不变（只是内容被替换）
    assert len(result) == original_len
    # micro_compact 应该有执行
    assert compressor.stats["micro_compact_count"] > 0
    # auto_compact 不应触发
    assert compressor.stats["auto_compact_count"] == 0


def test_compress_before_call_triggers_auto():
    """超过阈值时触发 auto_compact"""
    compressor = ContextCompressor(
        token_threshold=10,  # 极低阈值
        keep_recent=1,
    )
    messages = _make_messages(3)

    result = asyncio.run(compressor.compress_before_call(messages))

    # auto_compact 应该触发
    assert compressor.stats["auto_compact_count"] == 1
    # 消息被压缩
    assert len(result) <= 3


# ---------------------------------------------------------------------------
# manual_compact
# ---------------------------------------------------------------------------

def test_manual_compact():
    """手动触发压缩"""
    compressor = ContextCompressor()
    messages = _make_messages(3)

    result = asyncio.run(compressor.manual_compact(messages, focus="重点关注测试"))

    assert len(result) <= 3
    assert compressor.stats["auto_compact_count"] == 1


# ---------------------------------------------------------------------------
# 规则摘要
# ---------------------------------------------------------------------------

def test_rule_based_summary():
    """规则摘要包含关键信息"""
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "帮我写一个排序算法"},
        {"role": "assistant", "content": "好的，这是冒泡排序的实现"},
        {"role": "tool", "tool_call_id": "c1", "content": "文件已保存"},
        {"role": "user", "content": "再优化一下性能"},
    ]
    summary = ContextCompressor._rule_based_summary(messages)
    assert "排序" in summary
    assert "用户请求" in summary


def test_append_missing_identifiers():
    messages = [
        {"role": "user", "content": "请检查 `/tmp/demo.py` 里的 `build_index`，task-run-12 失败了"},
        {"role": "assistant", "content": "我会检查"},
    ]
    summary = ContextCompressor._append_missing_identifiers(messages, "已经完成分析。")
    assert "/tmp/demo.py" in summary
    assert "build_index" in summary
    assert "task-run-12" in summary


def test_build_tool_name_map():
    """从 assistant 消息中提取 tool_call_id → tool_name 映射"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "bash", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "test"},
    ]
    mapping = ContextCompressor._build_tool_name_map(messages)
    assert mapping["c1"] == "read_file"
    assert mapping["c2"] == "bash"


# ---------------------------------------------------------------------------
# 百分比阈值 + 断路器
# ---------------------------------------------------------------------------

def test_context_window_based_threshold():
    """context_window_tokens 参数应根据百分比计算阈值"""
    compressor = ContextCompressor(context_window_tokens=128_000)
    # 128K * 0.80 - 13K = 89_400
    assert compressor._token_threshold == 128_000 * 0.80 - 13_000

    # 小窗口模型
    compressor_small = ContextCompressor(context_window_tokens=32_000)
    assert compressor_small._token_threshold == 32_000 * 0.80 - 13_000


def test_context_window_zero_uses_default():
    """context_window_tokens=0 时使用固定默认值"""
    compressor = ContextCompressor(context_window_tokens=0, token_threshold=80_000)
    assert compressor._token_threshold == 80_000


def test_circuit_breaker_stops_compact_after_failures():
    """连续压缩失败达到上限后跳过压缩"""
    compressor = ContextCompressor(token_threshold=10, keep_recent=1)
    messages = _make_messages(3)

    # 模拟连续失败
    compressor._consecutive_compact_failures = 3

    result = asyncio.run(compressor.compress_before_call(list(messages)))

    # 断路器生效: auto_compact 不应触发
    assert compressor.stats["auto_compact_count"] == 0
    # 消息不应被压缩（只有 micro_compact）
    assert len(result) == len(messages)


def test_circuit_breaker_resets_on_success():
    """压缩成功后断路器重置"""
    compressor = ContextCompressor(token_threshold=10, keep_recent=1)
    compressor._consecutive_compact_failures = 2

    messages = _make_messages(3)
    asyncio.run(compressor.compress_before_call(list(messages)))

    # 成功后重置
    assert compressor._consecutive_compact_failures == 0
