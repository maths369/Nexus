"""
Tool Loop Detection — 工具调用循环检测

对标 OpenClaw src/agents/tool-loop-detection.ts 的完整 Python 实现。

四层防护:
1. generic_repeat     — 任意工具+参数被重复调用 N 次（warn）
2. known_poll_no_progress — 已知轮询工具连续无进展（warn → block）
3. ping_pong          — 两个工具交替调用无进展（warn → block）
4. global_circuit_breaker — 任意工具重复 N 次的紧急熔断（block）

每层独立可配置，所有检测默认启用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（对标 OpenClaw）
# ---------------------------------------------------------------------------

TOOL_CALL_HISTORY_SIZE = 30
WARNING_THRESHOLD = 10
CRITICAL_THRESHOLD = 20
GLOBAL_CIRCUIT_BREAKER_THRESHOLD = 30

# 警告桶大小：每 N 次只输出一次警告，避免日志刷屏
LOOP_WARNING_BUCKET_SIZE = 10

# 已知轮询工具模式
_KNOWN_POLL_TOOLS: set[str] = {
    "command_status",
}
_KNOWN_POLL_ACTIONS: dict[str, set[str]] = {
    "process": {"poll", "log"},
}


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------

class LoopDetectorKind(str, Enum):
    GENERIC_REPEAT = "generic_repeat"
    KNOWN_POLL_NO_PROGRESS = "known_poll_no_progress"
    PING_PONG = "ping_pong"
    GLOBAL_CIRCUIT_BREAKER = "global_circuit_breaker"


class LoopSeverity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class LoopDetectionConfig:
    """检测配置，对标 OpenClaw LoopDetectionConfig。"""
    enabled: bool = True
    history_size: int = TOOL_CALL_HISTORY_SIZE
    warning_threshold: int = WARNING_THRESHOLD
    critical_threshold: int = CRITICAL_THRESHOLD
    global_circuit_breaker_threshold: int = GLOBAL_CIRCUIT_BREAKER_THRESHOLD
    detectors: dict[LoopDetectorKind, bool] = field(default_factory=lambda: {
        LoopDetectorKind.GENERIC_REPEAT: True,
        LoopDetectorKind.KNOWN_POLL_NO_PROGRESS: True,
        LoopDetectorKind.PING_PONG: True,
        LoopDetectorKind.GLOBAL_CIRCUIT_BREAKER: True,
    })


@dataclass
class ToolCallRecord:
    """滑动窗口中的一条工具调用记录。"""
    tool_name: str
    args_hash: str
    tool_call_id: str
    timestamp: float
    outcome_hash: str | None = None  # 执行完成后填入


@dataclass
class LoopDetectionResult:
    """检测结果。"""
    stuck: bool
    severity: LoopSeverity | None = None
    kind: LoopDetectorKind | None = None
    tool_name: str = ""
    count: int = 0
    message: str = ""


@dataclass
class LoopDetectionState:
    """
    每个 agent run 独立的检测状态。
    对标 OpenClaw ToolLoopDetectionState。
    """
    history: list[ToolCallRecord] = field(default_factory=list)
    warning_buckets: dict[str, int] = field(default_factory=dict)

    def clear(self) -> None:
        self.history.clear()
        self.warning_buckets.clear()


@dataclass
class ToolCallStats:
    """统计信息，用于调试/监控。"""
    total_calls: int = 0
    unique_patterns: int = 0
    most_frequent_tool: str = ""
    most_frequent_count: int = 0


# ---------------------------------------------------------------------------
# Hash 函数
# ---------------------------------------------------------------------------

def hash_tool_call(tool_name: str, params: dict[str, Any]) -> str:
    """生成 tool_name + 参数的稳定 SHA256 hash（对标 OpenClaw hashToolCall）。"""
    try:
        params_str = json.dumps(params, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        params_str = str(params)
    raw = f"{tool_name}:{params_str}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def hash_tool_outcome(
    tool_name: str,
    params: dict[str, Any],
    output: str | None = None,
    error: str | None = None,
) -> str:
    """
    生成工具调用结果的 hash，用于检测 no-progress。
    对标 OpenClaw hashToolOutcome。
    """
    parts = [hash_tool_call(tool_name, params)]
    if error:
        parts.append(f"error:{error[:200]}")
    elif output:
        # 对长输出取摘要
        text = output[:2000] if len(output) > 2000 else output
        parts.append(f"ok:{text}")
    else:
        parts.append("ok:empty")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 核心 API
# ---------------------------------------------------------------------------

def record_tool_call(
    state: LoopDetectionState,
    tool_name: str,
    params: dict[str, Any],
    tool_call_id: str,
    config: LoopDetectionConfig | None = None,
) -> None:
    """记录一次工具调用到滑动窗口（对标 OpenClaw recordToolCall）。"""
    cfg = config or LoopDetectionConfig()
    args_hash = hash_tool_call(tool_name, params)
    record = ToolCallRecord(
        tool_name=tool_name,
        args_hash=args_hash,
        tool_call_id=tool_call_id,
        timestamp=time.monotonic(),
    )
    state.history.append(record)
    # 维护滑动窗口
    if len(state.history) > cfg.history_size:
        state.history = state.history[-cfg.history_size:]


def record_tool_call_outcome(
    state: LoopDetectionState,
    tool_call_id: str,
    tool_name: str,
    params: dict[str, Any],
    output: str | None = None,
    error: str | None = None,
) -> None:
    """记录工具执行结果（对标 OpenClaw recordToolCallOutcome）。"""
    outcome = hash_tool_outcome(tool_name, params, output=output, error=error)
    # 优先按 tool_call_id 匹配，回退按 args_hash
    args_hash = hash_tool_call(tool_name, params)
    for rec in reversed(state.history):
        if rec.tool_call_id == tool_call_id:
            rec.outcome_hash = outcome
            return
        if rec.tool_name == tool_name and rec.args_hash == args_hash and rec.outcome_hash is None:
            rec.outcome_hash = outcome
            return


def detect_tool_call_loop(
    state: LoopDetectionState,
    tool_name: str,
    params: dict[str, Any],
    config: LoopDetectionConfig | None = None,
) -> LoopDetectionResult:
    """
    主检测入口（对标 OpenClaw detectToolCallLoop）。

    按优先级检查:
    1. Global circuit breaker (≥30) → critical
    2. Known poll no-progress critical (≥20) → critical
    3. Known poll no-progress warning (≥10) → warning
    4. Ping-pong critical (≥20) → critical
    5. Ping-pong warning (≥10) → warning
    6. Generic repeat (≥10) → warning
    """
    cfg = config or LoopDetectionConfig()
    if not cfg.enabled:
        return LoopDetectionResult(stuck=False)

    args_hash = hash_tool_call(tool_name, params)
    history = state.history

    # --- 1. Global circuit breaker ---
    if cfg.detectors.get(LoopDetectorKind.GLOBAL_CIRCUIT_BREAKER, True):
        repeat_count = _count_consecutive_same_args(history, tool_name, args_hash)
        if repeat_count >= cfg.global_circuit_breaker_threshold:
            return LoopDetectionResult(
                stuck=True,
                severity=LoopSeverity.CRITICAL,
                kind=LoopDetectorKind.GLOBAL_CIRCUIT_BREAKER,
                tool_name=tool_name,
                count=repeat_count,
                message=(
                    f"全局熔断：工具 {tool_name} 已用相同参数调用 {repeat_count} 次且无进展。"
                    "会话执行被阻断以防止资源浪费。"
                ),
            )

    # --- 2/3. Known poll no-progress ---
    if cfg.detectors.get(LoopDetectorKind.KNOWN_POLL_NO_PROGRESS, True):
        if _is_known_poll_tool(tool_name, params):
            no_progress_count = _count_no_progress_calls(history, tool_name, args_hash)
            if no_progress_count >= cfg.critical_threshold:
                return LoopDetectionResult(
                    stuck=True,
                    severity=LoopSeverity.CRITICAL,
                    kind=LoopDetectorKind.KNOWN_POLL_NO_PROGRESS,
                    tool_name=tool_name,
                    count=no_progress_count,
                    message=(
                        f"工具 {tool_name} 已连续 {no_progress_count} 次轮询且无进展。"
                        "请停止重试，改为向用户报告当前状态。"
                    ),
                )
            if no_progress_count >= cfg.warning_threshold:
                return LoopDetectionResult(
                    stuck=True,
                    severity=LoopSeverity.WARNING,
                    kind=LoopDetectorKind.KNOWN_POLL_NO_PROGRESS,
                    tool_name=tool_name,
                    count=no_progress_count,
                    message=(
                        f"工具 {tool_name} 已连续 {no_progress_count} 次轮询且无进展。"
                        "如果没有进展，请停止重试。"
                    ),
                )

    # --- 4/5. Ping-pong detection ---
    if cfg.detectors.get(LoopDetectorKind.PING_PONG, True):
        pp = _get_ping_pong_streak(history, tool_name, args_hash)
        if pp is not None:
            if pp.count >= cfg.critical_threshold:
                return LoopDetectionResult(
                    stuck=True,
                    severity=LoopSeverity.CRITICAL,
                    kind=LoopDetectorKind.PING_PONG,
                    tool_name=tool_name,
                    count=pp.count,
                    message=(
                        f"检测到工具 {pp.tool_a} 和 {pp.tool_b} 交替调用 {pp.count} 次且无进展。"
                        "请停止此模式，尝试不同方法。"
                    ),
                )
            if pp.count >= cfg.warning_threshold:
                return LoopDetectionResult(
                    stuck=True,
                    severity=LoopSeverity.WARNING,
                    kind=LoopDetectorKind.PING_PONG,
                    tool_name=tool_name,
                    count=pp.count,
                    message=(
                        f"检测到工具 {pp.tool_a} 和 {pp.tool_b} 交替调用 {pp.count} 次。"
                        "如果没有进展，请尝试不同方法。"
                    ),
                )

    # --- 6. Generic repeat ---
    if cfg.detectors.get(LoopDetectorKind.GENERIC_REPEAT, True):
        repeat_count = _count_consecutive_same_args(history, tool_name, args_hash)
        if repeat_count >= cfg.warning_threshold:
            return LoopDetectionResult(
                stuck=True,
                severity=LoopSeverity.WARNING,
                kind=LoopDetectorKind.GENERIC_REPEAT,
                tool_name=tool_name,
                count=repeat_count,
                message=(
                    f"工具 {tool_name} 已用相同参数调用 {repeat_count} 次。"
                    "如果没有进展，请停止重试，改为向用户说明遇到的困难。"
                ),
            )

    return LoopDetectionResult(stuck=False)


def should_emit_loop_warning(
    state: LoopDetectionState,
    result: LoopDetectionResult,
) -> bool:
    """
    警告节流：同一检测结果只在桶边界输出一次（对标 OpenClaw shouldEmitLoopWarning）。
    """
    if not result.stuck or result.severity != LoopSeverity.WARNING:
        return False
    bucket_key = f"{result.kind}:{result.tool_name}"
    current_bucket = result.count // LOOP_WARNING_BUCKET_SIZE
    last_bucket = state.warning_buckets.get(bucket_key, -1)
    if current_bucket > last_bucket:
        state.warning_buckets[bucket_key] = current_bucket
        return True
    return False


def get_tool_call_stats(state: LoopDetectionState) -> ToolCallStats:
    """返回统计信息（对标 OpenClaw getToolCallStats）。"""
    if not state.history:
        return ToolCallStats()
    freq: dict[str, int] = {}
    patterns: set[str] = set()
    for rec in state.history:
        freq[rec.tool_name] = freq.get(rec.tool_name, 0) + 1
        patterns.add(f"{rec.tool_name}:{rec.args_hash}")
    most_tool = max(freq, key=freq.get)  # type: ignore[arg-type]
    return ToolCallStats(
        total_calls=len(state.history),
        unique_patterns=len(patterns),
        most_frequent_tool=most_tool,
        most_frequent_count=freq[most_tool],
    )


# ---------------------------------------------------------------------------
# 内部检测函数
# ---------------------------------------------------------------------------

def _count_consecutive_same_args(
    history: list[ToolCallRecord],
    tool_name: str,
    args_hash: str,
) -> int:
    """从历史末尾往前数，同 tool+args 的连续调用次数。"""
    count = 0
    for rec in reversed(history):
        if rec.tool_name == tool_name and rec.args_hash == args_hash:
            count += 1
        else:
            break
    return count


def _count_no_progress_calls(
    history: list[ToolCallRecord],
    tool_name: str,
    args_hash: str,
) -> int:
    """
    从历史末尾往前数，同 tool+args 且 outcome_hash 相同（无进展）的连续调用次数。
    只有当 outcome_hash 都存在且相同时才计入。
    """
    # 找到最近一次的 outcome_hash 作为基准
    baseline_outcome: str | None = None
    for rec in reversed(history):
        if rec.tool_name == tool_name and rec.args_hash == args_hash:
            if rec.outcome_hash is not None:
                baseline_outcome = rec.outcome_hash
                break
    if baseline_outcome is None:
        return 0

    count = 0
    for rec in reversed(history):
        if rec.tool_name == tool_name and rec.args_hash == args_hash:
            if rec.outcome_hash == baseline_outcome:
                count += 1
            elif rec.outcome_hash is not None:
                # 不同的 outcome → 有进展，停止计数
                break
            else:
                # outcome 尚未记录，保守地继续
                count += 1
        else:
            break
    return count


def _is_known_poll_tool(tool_name: str, params: dict[str, Any]) -> bool:
    """判断是否为已知轮询工具（对标 OpenClaw isKnownPollTool）。"""
    if tool_name in _KNOWN_POLL_TOOLS:
        return True
    action = params.get("action", "")
    if tool_name in _KNOWN_POLL_ACTIONS:
        return action in _KNOWN_POLL_ACTIONS[tool_name]
    return False


@dataclass
class _PingPongInfo:
    tool_a: str
    tool_b: str
    count: int


def _get_ping_pong_streak(
    history: list[ToolCallRecord],
    current_tool: str,
    current_args_hash: str,
) -> _PingPongInfo | None:
    """
    检测 A→B→A→B 交替调用模式（对标 OpenClaw getPingPongStreak）。

    要求:
    - 交替尾部 ≥ 2 条
    - 两侧都有稳定的 outcome（相同 outcome_hash）
    - 当前调用匹配预期的下一个
    """
    if len(history) < 4:
        return None

    # 从尾部向前找最后两个不同的 tool+args 模式
    last = history[-1]
    second_last: ToolCallRecord | None = None
    for rec in reversed(history[:-1]):
        if rec.args_hash != last.args_hash or rec.tool_name != last.tool_name:
            second_last = rec
            break

    if second_last is None:
        return None

    # 检查交替模式
    pattern_a = (last.tool_name, last.args_hash)
    pattern_b = (second_last.tool_name, second_last.args_hash)

    # 当前调用必须匹配交替中的下一个
    current_pattern = (current_tool, current_args_hash)
    # 最后一个是 A，下一个应该是 B
    expected_next = pattern_b if (last.tool_name, last.args_hash) == pattern_a else pattern_a
    if current_pattern != expected_next:
        return None

    # 从尾部向前数交替长度
    streak = 0
    expect_a = True  # 最后一条是 pattern_a
    outcomes_a: set[str] = set()
    outcomes_b: set[str] = set()

    for rec in reversed(history):
        expected = pattern_a if expect_a else pattern_b
        if (rec.tool_name, rec.args_hash) == expected:
            streak += 1
            if expect_a and rec.outcome_hash:
                outcomes_a.add(rec.outcome_hash)
            elif not expect_a and rec.outcome_hash:
                outcomes_b.add(rec.outcome_hash)
            expect_a = not expect_a
        else:
            break

    if streak < 2:
        return None

    # 两侧都应该有稳定的 outcome（只有 1 种 outcome_hash）
    a_stable = len(outcomes_a) <= 1 and len(outcomes_a) > 0
    b_stable = len(outcomes_b) <= 1 and len(outcomes_b) > 0
    if not (a_stable and b_stable):
        return None

    return _PingPongInfo(
        tool_a=pattern_a[0],
        tool_b=pattern_b[0],
        count=streak,
    )
