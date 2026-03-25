"""
Tool Loop Detection 测试

对标 OpenClaw src/agents/tool-loop-detection.test.ts
"""

from __future__ import annotations

from nexus.agent.tool_loop_detection import (
    LoopDetectionConfig,
    LoopDetectionState,
    LoopDetectorKind,
    LoopSeverity,
    detect_tool_call_loop,
    get_tool_call_stats,
    hash_tool_call,
    hash_tool_outcome,
    record_tool_call,
    record_tool_call_outcome,
    should_emit_loop_warning,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _record_calls(
    state: LoopDetectionState,
    tool_name: str,
    params: dict,
    count: int,
    *,
    config: LoopDetectionConfig | None = None,
    output: str = "ok",
    error: str | None = None,
) -> None:
    """批量记录工具调用 + outcome。"""
    cfg = config or LoopDetectionConfig()
    for i in range(count):
        call_id = f"call-{tool_name}-{i}"
        record_tool_call(state, tool_name, params, call_id, cfg)
        record_tool_call_outcome(
            state, call_id, tool_name, params,
            output=output if error is None else None,
            error=error,
        )


def _record_ping_pong(
    state: LoopDetectionState,
    tool_a: str, params_a: dict,
    tool_b: str, params_b: dict,
    count: int,
    *,
    config: LoopDetectionConfig | None = None,
    output_a: str = "result_a",
    output_b: str = "result_b",
) -> None:
    """批量记录交替调用。"""
    cfg = config or LoopDetectionConfig()
    for i in range(count):
        if i % 2 == 0:
            call_id = f"call-a-{i}"
            record_tool_call(state, tool_a, params_a, call_id, cfg)
            record_tool_call_outcome(state, call_id, tool_a, params_a, output=output_a)
        else:
            call_id = f"call-b-{i}"
            record_tool_call(state, tool_b, params_b, call_id, cfg)
            record_tool_call_outcome(state, call_id, tool_b, params_b, output=output_b)


# ---------------------------------------------------------------------------
# Hash 测试
# ---------------------------------------------------------------------------

class TestHashToolCall:
    def test_consistent_hashing(self):
        h1 = hash_tool_call("read_vault", {"path": "a.md"})
        h2 = hash_tool_call("read_vault", {"path": "a.md"})
        assert h1 == h2

    def test_different_params_different_hash(self):
        h1 = hash_tool_call("read_vault", {"path": "a.md"})
        h2 = hash_tool_call("read_vault", {"path": "b.md"})
        assert h1 != h2

    def test_different_tools_different_hash(self):
        h1 = hash_tool_call("read_vault", {"path": "a.md"})
        h2 = hash_tool_call("write_vault", {"path": "a.md"})
        assert h1 != h2

    def test_key_order_independent(self):
        h1 = hash_tool_call("tool", {"a": 1, "b": 2})
        h2 = hash_tool_call("tool", {"b": 2, "a": 1})
        assert h1 == h2

    def test_fixed_output_size(self):
        h = hash_tool_call("tool", {"data": "x" * 20000})
        assert len(h) == 16


class TestHashToolOutcome:
    def test_same_output_same_hash(self):
        h1 = hash_tool_outcome("tool", {"a": 1}, output="result")
        h2 = hash_tool_outcome("tool", {"a": 1}, output="result")
        assert h1 == h2

    def test_different_output_different_hash(self):
        h1 = hash_tool_outcome("tool", {"a": 1}, output="result_1")
        h2 = hash_tool_outcome("tool", {"a": 1}, output="result_2")
        assert h1 != h2

    def test_error_vs_success_different_hash(self):
        h1 = hash_tool_outcome("tool", {"a": 1}, output="ok")
        h2 = hash_tool_outcome("tool", {"a": 1}, error="FileNotFoundError")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Record 测试
# ---------------------------------------------------------------------------

class TestRecordToolCall:
    def test_adds_to_empty_history(self):
        state = LoopDetectionState()
        record_tool_call(state, "tool", {"a": 1}, "call-1")
        assert len(state.history) == 1
        assert state.history[0].tool_name == "tool"

    def test_maintains_sliding_window(self):
        config = LoopDetectionConfig(history_size=4)
        state = LoopDetectionState()
        for i in range(6):
            record_tool_call(state, "tool", {"i": i}, f"call-{i}", config)
        assert len(state.history) == 4
        # 最早的两条被丢弃
        assert state.history[0].tool_call_id == "call-2"

    def test_records_timestamp(self):
        state = LoopDetectionState()
        record_tool_call(state, "tool", {}, "call-1")
        assert state.history[0].timestamp > 0


class TestRecordOutcome:
    def test_matches_by_call_id(self):
        state = LoopDetectionState()
        record_tool_call(state, "tool", {"a": 1}, "call-1")
        record_tool_call_outcome(state, "call-1", "tool", {"a": 1}, output="ok")
        assert state.history[0].outcome_hash is not None

    def test_falls_back_to_args_hash(self):
        state = LoopDetectionState()
        record_tool_call(state, "tool", {"a": 1}, "call-1")
        # 用不同的 call_id 但相同的 tool+args
        record_tool_call_outcome(state, "call-999", "tool", {"a": 1}, output="ok")
        assert state.history[0].outcome_hash is not None


# ---------------------------------------------------------------------------
# Loop 检测测试
# ---------------------------------------------------------------------------

class TestDetectDisabled:
    def test_disabled_returns_not_stuck(self):
        config = LoopDetectionConfig(enabled=False)
        state = LoopDetectionState()
        _record_calls(state, "tool", {"a": 1}, 50, config=config)
        result = detect_tool_call_loop(state, "tool", {"a": 1}, config)
        assert not result.stuck


class TestGenericRepeat:
    def test_no_loop_for_unique_calls(self):
        state = LoopDetectionState()
        for i in range(15):
            record_tool_call(state, "tool", {"i": i}, f"call-{i}")
            record_tool_call_outcome(state, f"call-{i}", "tool", {"i": i}, output=f"r{i}")
        result = detect_tool_call_loop(state, "tool", {"i": 99})
        assert not result.stuck

    def test_warning_at_threshold(self):
        state = LoopDetectionState()
        _record_calls(state, "read_vault", {"path": "a.md"}, 10)
        result = detect_tool_call_loop(state, "read_vault", {"path": "a.md"})
        assert result.stuck
        assert result.severity == LoopSeverity.WARNING
        assert result.kind == LoopDetectorKind.GENERIC_REPEAT
        assert result.count == 10

    def test_stays_warning_below_circuit_breaker(self):
        state = LoopDetectionState()
        _record_calls(state, "read_vault", {"path": "a.md"}, 25)
        result = detect_tool_call_loop(state, "read_vault", {"path": "a.md"})
        assert result.stuck
        assert result.severity == LoopSeverity.WARNING
        assert result.kind == LoopDetectorKind.GENERIC_REPEAT

    def test_custom_thresholds(self):
        config = LoopDetectionConfig(warning_threshold=2, critical_threshold=4)
        state = LoopDetectionState()
        _record_calls(state, "tool", {"a": 1}, 2, config=config)
        result = detect_tool_call_loop(state, "tool", {"a": 1}, config)
        assert result.stuck
        assert result.severity == LoopSeverity.WARNING

    def test_detector_can_be_disabled(self):
        config = LoopDetectionConfig(
            detectors={
                LoopDetectorKind.GENERIC_REPEAT: False,
                LoopDetectorKind.KNOWN_POLL_NO_PROGRESS: True,
                LoopDetectorKind.PING_PONG: True,
                LoopDetectorKind.GLOBAL_CIRCUIT_BREAKER: True,
            },
        )
        state = LoopDetectionState()
        _record_calls(state, "tool", {"a": 1}, 15, config=config)
        result = detect_tool_call_loop(state, "tool", {"a": 1}, config)
        # generic_repeat disabled, not enough for circuit breaker
        assert not result.stuck


class TestKnownPollNoProgress:
    def test_warning_at_threshold(self):
        state = LoopDetectionState()
        _record_calls(state, "command_status", {"id": "cmd1"}, 10, output="running")
        result = detect_tool_call_loop(state, "command_status", {"id": "cmd1"})
        assert result.stuck
        assert result.severity == LoopSeverity.WARNING
        assert result.kind == LoopDetectorKind.KNOWN_POLL_NO_PROGRESS

    def test_critical_at_threshold(self):
        state = LoopDetectionState()
        _record_calls(state, "command_status", {"id": "cmd1"}, 20, output="running")
        result = detect_tool_call_loop(state, "command_status", {"id": "cmd1"})
        assert result.stuck
        assert result.severity == LoopSeverity.CRITICAL
        assert result.kind == LoopDetectorKind.KNOWN_POLL_NO_PROGRESS

    def test_allowed_if_output_changes(self):
        """轮询结果变化 → known_poll_no_progress 不触发（但 generic_repeat 仍然可能触发）。"""
        state = LoopDetectionState()
        for i in range(12):
            call_id = f"call-{i}"
            record_tool_call(state, "command_status", {"id": "cmd1"}, call_id)
            record_tool_call_outcome(
                state, call_id, "command_status", {"id": "cmd1"},
                output=f"progress_{i}",
            )
        result = detect_tool_call_loop(state, "command_status", {"id": "cmd1"})
        # known_poll_no_progress 不触发（有进展），但 generic_repeat 可能触发
        assert result.kind != LoopDetectorKind.KNOWN_POLL_NO_PROGRESS

    def test_process_poll_action(self):
        """process 工具的 poll action 也是已知轮询。"""
        state = LoopDetectionState()
        params = {"action": "poll", "pid": 123}
        _record_calls(state, "process", params, 10, output="running")
        result = detect_tool_call_loop(state, "process", params)
        assert result.stuck
        assert result.kind == LoopDetectorKind.KNOWN_POLL_NO_PROGRESS


class TestGlobalCircuitBreaker:
    def test_triggers_at_threshold(self):
        state = LoopDetectionState()
        _record_calls(state, "any_tool", {"x": 1}, 30)
        result = detect_tool_call_loop(state, "any_tool", {"x": 1})
        assert result.stuck
        assert result.severity == LoopSeverity.CRITICAL
        assert result.kind == LoopDetectorKind.GLOBAL_CIRCUIT_BREAKER

    def test_overrides_other_detectors(self):
        """Global circuit breaker 优先级最高。"""
        state = LoopDetectionState()
        _record_calls(state, "command_status", {"id": "cmd1"}, 30, output="running")
        result = detect_tool_call_loop(state, "command_status", {"id": "cmd1"})
        assert result.kind == LoopDetectorKind.GLOBAL_CIRCUIT_BREAKER


class TestPingPong:
    def test_warning_at_threshold(self):
        state = LoopDetectionState()
        _record_ping_pong(
            state,
            "read_vault", {"path": "a.md"},
            "list_vault_pages", {"section": ""},
            count=12,
        )
        # 下一个调用应该是 read_vault（偶数索引）
        result = detect_tool_call_loop(state, "read_vault", {"path": "a.md"})
        assert result.stuck
        assert result.severity == LoopSeverity.WARNING
        assert result.kind == LoopDetectorKind.PING_PONG

    def test_critical_at_threshold(self):
        state = LoopDetectionState()
        _record_ping_pong(
            state,
            "read_vault", {"path": "a.md"},
            "list_vault_pages", {"section": ""},
            count=22,
        )
        result = detect_tool_call_loop(state, "read_vault", {"path": "a.md"})
        assert result.stuck
        assert result.severity == LoopSeverity.CRITICAL
        assert result.kind == LoopDetectorKind.PING_PONG

    def test_not_triggered_if_outcomes_vary(self):
        """交替调用但结果不同 → 有进展，不触发。"""
        state = LoopDetectionState()
        config = LoopDetectionConfig()
        for i in range(12):
            if i % 2 == 0:
                call_id = f"call-a-{i}"
                record_tool_call(state, "tool_a", {"a": 1}, call_id, config)
                record_tool_call_outcome(state, call_id, "tool_a", {"a": 1}, output=f"vary_{i}")
            else:
                call_id = f"call-b-{i}"
                record_tool_call(state, "tool_b", {"b": 1}, call_id, config)
                record_tool_call_outcome(state, call_id, "tool_b", {"b": 1}, output=f"vary_{i}")
        result = detect_tool_call_loop(state, "tool_a", {"a": 1}, config)
        assert not result.stuck

    def test_broken_by_third_tool(self):
        """交替模式被第三个工具打断 → 不触发。"""
        state = LoopDetectionState()
        _record_ping_pong(
            state,
            "tool_a", {"a": 1},
            "tool_b", {"b": 1},
            count=8,
        )
        # 插入第三个工具打断
        record_tool_call(state, "tool_c", {"c": 1}, "break-call")
        record_tool_call_outcome(state, "break-call", "tool_c", {"c": 1}, output="break")
        result = detect_tool_call_loop(state, "tool_a", {"a": 1})
        assert not result.stuck


class TestWarningThrottling:
    def test_emits_on_bucket_boundary(self):
        state = LoopDetectionState()
        _record_calls(state, "tool", {"a": 1}, 10)
        result = detect_tool_call_loop(state, "tool", {"a": 1})
        assert should_emit_loop_warning(state, result)

    def test_suppresses_within_same_bucket(self):
        state = LoopDetectionState()
        _record_calls(state, "tool", {"a": 1}, 10)
        result = detect_tool_call_loop(state, "tool", {"a": 1})
        # 第一次 emit
        assert should_emit_loop_warning(state, result)
        # 同一桶内再次检查 → 抑制
        assert not should_emit_loop_warning(state, result)

    def test_emits_again_at_next_bucket(self):
        state = LoopDetectionState()
        _record_calls(state, "tool", {"a": 1}, 10)
        result_10 = detect_tool_call_loop(state, "tool", {"a": 1})
        should_emit_loop_warning(state, result_10)

        # 继续到 20
        _record_calls(state, "tool", {"a": 1}, 10)
        result_20 = detect_tool_call_loop(state, "tool", {"a": 1})
        assert should_emit_loop_warning(state, result_20)


# ---------------------------------------------------------------------------
# Stats 测试
# ---------------------------------------------------------------------------

class TestStats:
    def test_empty_history(self):
        state = LoopDetectionState()
        stats = get_tool_call_stats(state)
        assert stats.total_calls == 0

    def test_counts_correctly(self):
        state = LoopDetectionState()
        for i in range(5):
            record_tool_call(state, "tool_a", {"i": i}, f"a-{i}")
        for i in range(3):
            record_tool_call(state, "tool_b", {"i": i}, f"b-{i}")
        stats = get_tool_call_stats(state)
        assert stats.total_calls == 8
        assert stats.unique_patterns == 8  # 每次参数不同
        assert stats.most_frequent_tool == "tool_a"
        assert stats.most_frequent_count == 5

    def test_identifies_most_frequent(self):
        state = LoopDetectionState()
        _record_calls(state, "tool_a", {"x": 1}, 3)
        _record_calls(state, "tool_b", {"y": 1}, 7)
        stats = get_tool_call_stats(state)
        assert stats.most_frequent_tool == "tool_b"
        assert stats.most_frequent_count == 7
