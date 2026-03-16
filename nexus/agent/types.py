"""Agent Core 数据类型定义"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Awaitable


# ---------------------------------------------------------------------------
# Run 状态机
# ---------------------------------------------------------------------------

class RunStatus(str, enum.Enum):
    """Run 生命周期状态"""
    QUEUED = "queued"           # 排队中
    PLANNING = "planning"      # 制定计划
    RUNNING = "running"        # 执行中
    WAITING = "waiting"        # 等待用户输入或审批
    VALIDATING = "validating"  # 验证结果
    SUCCEEDED = "succeeded"    # 成功（终态）
    FAILED = "failed"          # 失败（终态）


class ToolRiskLevel(str, enum.Enum):
    """工具风险等级"""
    LOW = "low"           # 无副作用（查询、读取）
    MEDIUM = "medium"     # 有限副作用（创建文件、发送消息）
    HIGH = "high"         # 重大副作用（删除、修改配置、安装）
    CRITICAL = "critical" # 需要人工审批（支付、权限变更）


# ---------------------------------------------------------------------------
# Tool 定义
# ---------------------------------------------------------------------------

@dataclass
class ToolDefinition:
    """工具定义"""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Awaitable[Any]]
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW
    # 是否需要审批
    requires_approval: bool = False
    # 工具类别标签
    tags: list[str] = field(default_factory=list)


@dataclass
class ToolCall:
    """LLM 请求的工具调用"""
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """工具执行结果"""
    call_id: str
    tool_name: str
    success: bool
    output: str
    error: str | None = None
    duration_ms: float = 0


# ---------------------------------------------------------------------------
# Run 定义
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """一次任务执行"""
    run_id: str
    session_id: str
    status: RunStatus = RunStatus.QUEUED
    task: str = ""                 # 任务描述
    plan: str = ""                 # 执行计划
    result: str = ""               # 最终结果
    error: str | None = None
    model: str = ""                # 使用的模型
    attempt_count: int = 0         # 已尝试次数
    max_attempts: int = 3          # 最大重试次数
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in (RunStatus.SUCCEEDED, RunStatus.FAILED)

    @property
    def can_retry(self) -> bool:
        return self.attempt_count < self.max_attempts


@dataclass
class RunEvent:
    """Run 执行过程中的事件"""
    event_id: str
    run_id: str
    event_type: str  # "status_change" | "tool_call" | "tool_result" | "llm_response" | "error"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Attempt 定义
# ---------------------------------------------------------------------------

@dataclass
class AttemptConfig:
    """单次执行的配置"""
    model: str
    system_prompt: str
    tools: list[ToolDefinition]
    messages: list[dict[str, Any]]  # OpenAI 兼容的 messages
    temperature: float = 0.7
    max_tokens: int = 4096
    # 流式输出回调
    stream_callback: Callable[[str], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Agent 异常
# ---------------------------------------------------------------------------

class ContextOverflowError(Exception):
    """上下文 token 溢出"""
    pass


class ProviderError(Exception):
    """LLM Provider 请求错误"""
    pass


class ToolExecutionError(Exception):
    """工具执行错误"""
    pass
