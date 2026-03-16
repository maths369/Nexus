"""Structured task payloads used for edge-node execution."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class TaskStepState(str, enum.Enum):
    QUEUED = "queued"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(slots=True)
class TaskAssignment:
    task_id: str
    step_id: str
    assigned_node: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "assigned_node": self.assigned_node,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "timeout_seconds": self.timeout_seconds,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskAssignment":
        return cls(
            task_id=str(data.get("task_id") or ""),
            step_id=str(data.get("step_id") or ""),
            assigned_node=str(data.get("assigned_node") or ""),
            tool_name=str(data.get("tool_name") or ""),
            arguments=dict(data.get("arguments") or {}),
            timeout_seconds=float(data.get("timeout_seconds") or 30.0),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class TaskExecutionResult:
    task_id: str
    step_id: str
    node_id: str
    tool_name: str
    success: bool
    output: str
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "node_id": self.node_id,
            "tool_name": self.tool_name,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskExecutionResult":
        return cls(
            task_id=str(data.get("task_id") or ""),
            step_id=str(data.get("step_id") or ""),
            node_id=str(data.get("node_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            success=bool(data.get("success")),
            output=str(data.get("output") or ""),
            error=str(data.get("error")) if data.get("error") is not None else None,
            duration_ms=float(data.get("duration_ms") or 0.0),
            metadata=dict(data.get("metadata") or {}),
        )


def task_assign_topic(task_id: str) -> str:
    return f"nexus/tasks/{task_id}/assign"


def task_status_topic(task_id: str) -> str:
    return f"nexus/tasks/{task_id}/status"


def task_result_topic(task_id: str) -> str:
    return f"nexus/tasks/{task_id}/result"
