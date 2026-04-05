"""Local macOS sidecar for the Nexus native shell."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nexus.agent.tools_policy import ToolsPolicy
from nexus.agent.types import RunEvent, ToolDefinition, ToolResult, ToolRiskLevel
from nexus.mesh import MQTTTransport, NodeCard
from nexus.mesh.task_protocol import TaskAssignment, TaskStepState
from nexus.services.browser import BrowserService, BrowserWorkerConfig, default_browser_worker_command
from nexus.services.workspace import WorkspaceService
from nexus.shared import NexusSettings, load_nexus_settings

from .agent import ApprovalRequestContext, EdgeNodeAgent
from .local_runtime import EdgeAgentRuntime, LocalRunResult, TaskJournal, build_edge_provider
from .tools import EdgeToolExecutor, build_edge_tool_registry

logger = logging.getLogger(__name__)


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _jsonable_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
        "risk_level": tool.risk_level.value,
        "requires_approval": tool.requires_approval,
        "tags": list(tool.tags),
    }


def _predict_load() -> float:
    cpu_count = os.cpu_count() or 1
    try:
        one_minute = os.getloadavg()[0]
    except (AttributeError, OSError):
        return 0.0
    return _clip(one_minute / max(cpu_count, 1), 0.0, 1.0)


def _parse_battery_percent(text: str) -> float | None:
    match = re.search(r"(\d+)%", text)
    if not match:
        return None
    return float(match.group(1))


def _read_battery_percent() -> float | None:
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return _parse_battery_percent(output)


async def _battery_level_provider() -> float | None:
    return await asyncio.to_thread(_read_battery_percent)


@dataclass(slots=True)
class SidecarEvent:
    timestamp: float
    kind: str
    level: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "level": self.level,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class TaskLogEntry:
    """Structured task log entry for the Task Log window."""
    task_id: str
    timestamp: float
    phase: str               # received | planned | dispatched | executing | tool_call | tool_result | completed | failed
    source: str              # feishu | desktop | hub | local
    node: str                # which node (hub, macbook-pro-yanglei, etc.)
    message: str             # human-readable description
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "phase": self.phase,
            "source": self.source,
            "node": self.node,
            "message": self.message,
            "details": self.details,
        }


class TaskLog:
    """In-memory task log with deque for recent entries."""
    def __init__(self, maxlen: int = 500) -> None:
        self._entries: deque[TaskLogEntry] = deque(maxlen=maxlen)

    def append(
        self,
        task_id: str,
        phase: str,
        source: str,
        node: str,
        message: str,
        **details: Any,
    ) -> TaskLogEntry:
        entry = TaskLogEntry(
            task_id=task_id,
            timestamp=time.time(),
            phase=phase,
            source=source,
            node=node,
            message=message,
            details=details,
        )
        self._entries.appendleft(entry)
        return entry

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return [e.to_dict() for e in list(self._entries)[:limit]]

    def by_task(self, task_id: str) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._entries if e.task_id == task_id]

    def clear(self) -> None:
        self._entries.clear()


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    requested_at: float
    tool_name: str
    risk_level: str
    reason: str
    arguments: dict[str, Any]
    source: str
    task_id: str | None = None
    step_id: str | None = None
    source_node: str | None = None
    timeout_seconds: float | None = None
    comment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "requested_at": self.requested_at,
            "tool_name": self.tool_name,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "arguments": self.arguments,
            "source": self.source,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "source_node": self.source_node,
            "timeout_seconds": self.timeout_seconds,
            "comment": self.comment,
        }


class ApprovalActionRequest(BaseModel):
    comment: str | None = None


class LocalCommandRequest(BaseModel):
    task: str
    system_prompt: str | None = None
    provider_name: str | None = None  # e.g. "minimax", "kimi", "mlx-local-api"


class EditorCompleteRequest(BaseModel):
    context_before: str = ""
    context_after: str = ""
    doc_path: str = ""
    max_tokens: int = 200
    language_hint: str = "auto"


class EditorTransformRequest(BaseModel):
    action: str  # translate | polish | expand | condense | rewrite
    selected_text: str
    context_before: str = ""
    context_after: str = ""
    doc_path: str = ""
    target_language: str = ""  # zh | en (for translate)


def _provider_configs_from_node_card(card: NodeCard) -> list[dict[str, Any]]:
    """Extract provider connection details from a NodeCard's ProviderSpec list."""
    configs: list[dict[str, Any]] = []
    for provider in card.providers:
        if provider.via not in ("api", "local"):
            continue
        props = dict(provider.properties or {})
        if provider.via == "local":
            # Local providers are exposed through an OpenAI-compatible endpoint.
            # Default to the lightweight MLX local API, while still allowing older
            # Ollama-style node cards to override the URL explicitly.
            configs.append({
                "name": provider.name,
                "model": provider.model,
                "provider": props.get("provider_type", "openai-compatible"),
                "base_url": props.get("base_url", "http://localhost:8008/v1"),
                "api_key": props.get("api_key", "mlx-local-api"),
                "api_key_env": props.get("api_key_env", ""),
            })
        else:
            configs.append({
                "name": provider.name,
                "model": provider.model,
                "provider": props.get("provider_type", provider.name),
                "base_url": props.get("base_url", ""),
                "api_key": props.get("api_key", ""),
                "api_key_env": props.get("api_key_env", ""),
            })
    return configs


@dataclass(slots=True)
class SidecarState:
    root_dir: Path
    http_host: str
    http_port: int
    tools: list[dict[str, Any]]
    mesh_summary: dict[str, Any]
    browser_enabled: bool
    phase: str = "stopped"
    transport_connected: bool = False
    hub_api_host: str = ""
    hub_api_port: int = 0
    hub_api_healthy: bool = False
    hub_runtime_ready: bool = False
    hub_node_online: bool = False
    hub_connectivity_state: str = "local_only"
    hub_reconnecting: bool = False
    hub_last_checked_at: float | None = None
    hub_last_error: str | None = None
    node_card: dict[str, Any] | None = None
    active_executions: int = 0
    started_at: float | None = None
    last_error: str | None = None
    recent_events: deque[SidecarEvent] = field(default_factory=lambda: deque(maxlen=50))
    pending_approvals: dict[str, PendingApproval] = field(default_factory=dict)
    task_log: TaskLog = field(default_factory=TaskLog)

    def set_phase(self, phase: str, *, error: str | None = None) -> None:
        self.phase = phase
        self.last_error = error
        if phase == "running" and self.started_at is None:
            self.started_at = time.time()

    def add_event(self, kind: str, message: str, *, level: str = "info", **details: Any) -> None:
        self.recent_events.appendleft(
            SidecarEvent(
                timestamp=time.time(),
                kind=kind,
                level=level,
                message=message,
                details=details,
            )
        )

    def recompute_hub_connectivity_state(self) -> str:
        if self.hub_reconnecting:
            self.hub_connectivity_state = "reconnecting"
        elif self.transport_connected and self.hub_api_healthy and self.hub_runtime_ready:
            self.hub_connectivity_state = "connected"
        elif self.transport_connected:
            self.hub_connectivity_state = "broker_only"
        else:
            self.hub_connectivity_state = "local_only"
        return self.hub_connectivity_state

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "transport_connected": self.transport_connected,
            "root_dir": str(self.root_dir),
            "http": {"host": self.http_host, "port": self.http_port},
            "browser": {"enabled": self.browser_enabled},
            "mesh": self.mesh_summary,
            "hub": {
                "api_host": self.hub_api_host,
                "api_port": self.hub_api_port,
                "api_healthy": self.hub_api_healthy,
                "runtime_ready": self.hub_runtime_ready,
                "hub_node_online": self.hub_node_online,
                "connectivity_state": self.hub_connectivity_state,
                "reconnecting": self.hub_reconnecting,
                "last_checked_at": self.hub_last_checked_at,
                "last_error": self.hub_last_error,
            },
            "node_card": self.node_card,
            "tools": self.tools,
            "active_executions": self.active_executions,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "pending_approvals": [
                approval.to_dict()
                for approval in sorted(
                    self.pending_approvals.values(),
                    key=lambda item: item.requested_at,
                    reverse=True,
                )
            ],
            "recent_events": [event.to_dict() for event in self.recent_events],
        }


class ApprovalManager:
    def __init__(self, state: SidecarState) -> None:
        self._state = state
        self._pending_futures: dict[str, asyncio.Future[tuple[bool, str | None]]] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        context: ApprovalRequestContext,
    ) -> None:
        approval_id = uuid.uuid4().hex[:12]
        approval = PendingApproval(
            approval_id=approval_id,
            requested_at=time.time(),
            tool_name=tool.name,
            risk_level=tool.risk_level.value,
            reason=f"Tool '{tool.name}' requires approval on this Mac",
            arguments=dict(arguments),
            source=context.source,
            task_id=context.task_id,
            step_id=context.step_id,
            source_node=context.source_node,
            timeout_seconds=context.timeout_seconds,
        )
        future: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()

        async with self._lock:
            self._state.pending_approvals[approval_id] = approval
            self._pending_futures[approval_id] = future

        self._state.add_event(
            "approval_needed",
            f"{tool.name} requires approval",
            level="warning",
            approval_id=approval_id,
            tool_name=tool.name,
            source=context.source,
            task_id=context.task_id,
            step_id=context.step_id,
        )

        try:
            approved, comment = await asyncio.wait_for(future, timeout=context.timeout_seconds or None)
        except TimeoutError as exc:
            await self._clear_pending(approval_id)
            self._state.add_event(
                "approval_timeout",
                f"Approval timed out for {tool.name}",
                level="error",
                approval_id=approval_id,
                tool_name=tool.name,
            )
            raise TimeoutError(f"Approval timed out for tool '{tool.name}'") from exc

        await self._clear_pending(approval_id)
        if not approved:
            raise PermissionError(comment or f"Approval rejected for tool '{tool.name}'")

    async def resolve(self, approval_id: str, *, approved: bool, comment: str | None = None) -> PendingApproval:
        async with self._lock:
            approval = self._state.pending_approvals.get(approval_id)
            future = self._pending_futures.get(approval_id)
            if approval is None or future is None:
                raise KeyError(approval_id)
            approval.comment = comment
            if not future.done():
                future.set_result((approved, comment))

        self._state.add_event(
            "approval_granted" if approved else "approval_rejected",
            f"{approval.tool_name} {'approved' if approved else 'rejected'}",
            level="info" if approved else "warning",
            approval_id=approval_id,
            tool_name=approval.tool_name,
            comment=comment,
        )
        return approval

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            approval.to_dict()
            for approval in sorted(
                self._state.pending_approvals.values(),
                key=lambda item: item.requested_at,
                reverse=True,
            )
        ]

    async def _clear_pending(self, approval_id: str) -> None:
        async with self._lock:
            self._state.pending_approvals.pop(approval_id, None)
            self._pending_futures.pop(approval_id, None)


class ObservableEdgeNodeAgent(EdgeNodeAgent):
    """Edge agent that mirrors runtime state into the local sidecar state."""

    def __init__(
        self,
        *args: Any,
        state: SidecarState,
        delegated_executor: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sidecar_state = state
        self._delegated_executor = delegated_executor

    async def start(self) -> None:
        self._sidecar_state.set_phase("starting")
        self._sidecar_state.hub_reconnecting = True
        self._sidecar_state.recompute_hub_connectivity_state()
        self._sidecar_state.add_event("transport", "Connecting to mesh broker")
        try:
            await super().start()
        except Exception as exc:
            self._sidecar_state.transport_connected = False
            self._sidecar_state.set_phase("error", error=str(exc))
            self._sidecar_state.add_event("error", "Failed to start edge agent", level="error", error=str(exc))
            raise
        self._sidecar_state.transport_connected = self._transport.connected
        self._sidecar_state.hub_reconnecting = False
        self._sidecar_state.recompute_hub_connectivity_state()
        self._sidecar_state.node_card = self._resolved_node_card().to_dict()
        self._sidecar_state.set_phase("running")
        self._sidecar_state.add_event("transport", "Edge agent started", node_id=self.node_id)

    async def stop(self) -> None:
        if self._started:
            self._sidecar_state.add_event("transport", "Stopping edge agent")
        await super().stop()
        self._sidecar_state.transport_connected = False
        self._sidecar_state.hub_reconnecting = False
        self._sidecar_state.recompute_hub_connectivity_state()
        self._sidecar_state.active_executions = 0
        self._sidecar_state.set_phase("stopped")

    async def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        self._sidecar_state.active_executions = self._active_executions + 1
        self._sidecar_state.add_event("tool", f"Executing {tool_name}", tool_name=tool_name)
        try:
            result = await super()._execute_tool(tool_name, arguments)
        finally:
            self._sidecar_state.active_executions = self._active_executions
        return result

    async def _publish_task_status(
        self,
        assignment: TaskAssignment,
        state: TaskStepState,
        *,
        error: str | None = None,
    ) -> None:
        await super()._publish_task_status(assignment, state, error=error)
        level = "error" if error else "warning" if state == TaskStepState.WAITING_APPROVAL else "info"
        self._sidecar_state.add_event(
            "task_status",
            f"{assignment.tool_name} -> {state.value}",
            level=level,
            task_id=assignment.task_id,
            step_id=assignment.step_id,
            tool_name=assignment.tool_name,
            error=error,
        )
        # Task Log
        self._sidecar_state.task_log.append(
            task_id=assignment.task_id,
            phase="executing" if state == TaskStepState.RUNNING else state.value,
            source="mqtt",
            node=self._node_card.node_id if self._node_card else "unknown",
            message=f"状态变更: {state.value}",
            step_id=assignment.step_id,
            tool_name=assignment.tool_name,
            error=error,
        )

    async def _publish_task_result(self, assignment: TaskAssignment, result: ToolResult) -> None:
        await super()._publish_task_result(assignment, result)
        level = "info" if result.success else "error"
        self._sidecar_state.add_event(
            "task_result",
            f"{assignment.tool_name} {'succeeded' if result.success else 'failed'}",
            level=level,
            task_id=assignment.task_id,
            step_id=assignment.step_id,
            tool_name=assignment.tool_name,
            success=result.success,
            error=result.error,
            duration_ms=result.duration_ms,
        )
        # Task Log
        self._sidecar_state.task_log.append(
            task_id=assignment.task_id,
            phase="completed" if result.success else "failed",
            source="mqtt",
            node=self._node_card.node_id if self._node_card else "unknown",
            message=f"{'✅ 完成' if result.success else '❌ 失败'}: {assignment.tool_name}",
            output_preview=result.output[:300] if result.output else "",
            error=result.error,
            duration_ms=result.duration_ms,
        )

    async def _execute_agent_loop(self, assignment: TaskAssignment) -> None:
        self._sidecar_state.add_event(
            "agent_loop",
            f"Starting agent-loop execution for task {assignment.task_id}",
            task_id=assignment.task_id,
            step_id=assignment.step_id,
        )
        # Task Log — record task received from Hub
        task_desc = str(assignment.metadata.get("task_description") or assignment.tool_name)
        self._sidecar_state.task_log.append(
            task_id=assignment.task_id,
            phase="received",
            source="hub",
            node=self._node_card.node_id if self._node_card else "unknown",
            message=f"📥 收到任务: {task_desc[:200]}",
            step_id=assignment.step_id,
            tool_name=assignment.tool_name,
        )
        if self._delegated_executor is not None:
            await self._publish_task_status(assignment, TaskStepState.RUNNING)
            self._active_executions += 1

            # Background heartbeat: send periodic progress so Hub knows we're alive
            heartbeat_done = asyncio.Event()

            async def _heartbeat():
                elapsed = 0
                while not heartbeat_done.is_set():
                    await asyncio.sleep(15.0)
                    if heartbeat_done.is_set():
                        break
                    elapsed += 15
                    try:
                        await self._publish_task_status(assignment, TaskStepState.RUNNING)
                        logger.debug("Task %s heartbeat (elapsed=%ds)", assignment.task_id, elapsed)
                    except Exception:
                        pass

            heartbeat_task = asyncio.create_task(_heartbeat())

            try:
                task_description = str(assignment.metadata.get("task_description") or assignment.tool_name)
                constraints = dict(assignment.metadata.get("constraints") or {})
                run_result = await self._delegated_executor(
                    task_description,
                    constraints if constraints else None,
                )
                result = ToolResult(
                    call_id=assignment.step_id or assignment.task_id,
                    tool_name=assignment.tool_name,
                    success=run_result.success,
                    output=run_result.output,
                    error=run_result.error,
                    duration_ms=run_result.duration_ms,
                )
            except Exception as exc:
                result = ToolResult(
                    call_id=assignment.step_id or assignment.task_id,
                    tool_name=assignment.tool_name,
                    success=False,
                    output="",
                    error=str(exc),
                )
            finally:
                heartbeat_done.set()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._active_executions = max(0, self._active_executions - 1)

            await self._publish_task_status(
                assignment,
                TaskStepState.SUCCEEDED if result.success else TaskStepState.FAILED,
                error=result.error,
            )
            await self._publish_task_result(assignment, result)
            return
        await super()._execute_agent_loop(assignment)

    async def _publish_offline(self) -> None:
        await super()._publish_offline()
        self._sidecar_state.add_event("transport", "Published offline event")

    async def _on_transport_recovery_attempt(self, exc: Exception) -> None:
        self._sidecar_state.transport_connected = False
        self._sidecar_state.hub_reconnecting = True
        self._sidecar_state.recompute_hub_connectivity_state()
        self._sidecar_state.set_phase("running", error=f"Hub reconnecting: {exc}")
        self._sidecar_state.add_event(
            "transport",
            "Mesh broker disconnected, reconnecting",
            level="warning",
            error=str(exc),
        )

    async def _on_transport_recovered(self) -> None:
        self._sidecar_state.transport_connected = self._transport.connected
        self._sidecar_state.hub_reconnecting = False
        self._sidecar_state.recompute_hub_connectivity_state()
        self._sidecar_state.set_phase("running")
        self._sidecar_state.add_event(
            "transport",
            "Mesh broker reconnected",
            node_id=self.node_id,
        )


async def _check_sidecar_permissions() -> list[dict[str, Any]]:
    """Check macOS permissions for this Python sidecar process.

    This is the process that actually runs AppleScript, screen capture, etc.
    The Swift app should call this endpoint to get the real permission status
    instead of checking its own process (which is a different executable).
    """
    import platform
    if platform.system() != "Darwin":
        return []

    checks: list[dict[str, Any]] = []

    # 1. Accessibility — run a harmless AX check via osascript
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c",
            "import ctypes; lib = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices'); "
            "print(lib.AXIsProcessTrusted())",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ax_trusted = stdout.decode().strip() == "1"
        checks.append({
            "id": "accessibility",
            "title": "Accessibility",
            "state": "granted" if ax_trusted else "needsPrompt",
            "detail": "Sidecar process has Accessibility access." if ax_trusted
                else "Sidecar process needs Accessibility permission.",
        })
    except Exception as exc:
        checks.append({
            "id": "accessibility",
            "title": "Accessibility",
            "state": "unavailable",
            "detail": f"Could not check: {exc}",
        })

    # 2. Screen Recording — try a minimal screen capture
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c",
            "import ctypes; cg = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'); "
            "print(cg.CGPreflightScreenCaptureAccess())",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        sc_granted = stdout.decode().strip() == "1"
        checks.append({
            "id": "screen_recording",
            "title": "Screen Recording",
            "state": "granted" if sc_granted else "needsPrompt",
            "detail": "Sidecar process has Screen Recording access." if sc_granted
                else "Sidecar process needs Screen Recording permission.",
        })
    except Exception as exc:
        checks.append({
            "id": "screen_recording",
            "title": "Screen Recording",
            "state": "unavailable",
            "detail": f"Could not check: {exc}",
        })

    # 3. Automation — test AppleScript for common targets
    for app_name, bundle_id in [("System Events", "com.apple.systemevents"), ("Google Chrome", "com.google.Chrome")]:
        try:
            # Simple AppleScript that returns immediately if permission is granted
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", f'tell application "System Events" to return name of first process whose bundle identifier is "{bundle_id}"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            stderr_text = stderr.decode().strip()
            if proc.returncode == 0:
                state = "granted"
                detail = f"Sidecar can send Apple Events to {app_name}."
            elif "not allowed" in stderr_text.lower() or "not permitted" in stderr_text.lower():
                state = "denied"
                detail = f"Sidecar denied Apple Events to {app_name}."
            else:
                state = "needsPrompt"
                detail = f"Sidecar needs Automation permission for {app_name}."
        except asyncio.TimeoutError:
            state = "needsPrompt"
            detail = f"Permission check timed out — likely waiting for user prompt for {app_name}."
        except Exception as exc:
            state = "unavailable"
            detail = f"Could not check: {exc}"

        checks.append({
            "id": f"automation.{app_name.lower().replace(' ', '_')}",
            "title": f"Automation · {app_name}",
            "state": state,
            "detail": detail,
        })

    return checks


def _clone_node_card(card: NodeCard) -> NodeCard:
    return NodeCard.from_dict(card.to_dict())


def _reconcile_node_card(card: NodeCard, tools: list[ToolDefinition], state: SidecarState) -> NodeCard:
    supported = {tool.name for tool in tools}
    tools_by_name = {tool.name: tool for tool in tools}
    adjusted = _clone_node_card(card)
    kept_capabilities = []

    # Track which tools are covered by existing capabilities
    covered_tools: set[str] = set()

    for capability in adjusted.capabilities:
        original = list(capability.tools)
        capability.tools = [tool for tool in capability.tools if tool in supported]
        removed = [tool for tool in original if tool not in capability.tools]
        if removed:
            state.add_event(
                "capability",
                f"Trimmed unsupported tools from {capability.capability_id}",
                level="warning",
                removed_tools=removed,
            )
        if capability.tools:
            kept_capabilities.append(capability)
            covered_tools.update(capability.tools)
        else:
            state.add_event(
                "capability",
                f"Dropped empty capability {capability.capability_id}",
                level="warning",
            )

    # Auto-discover: tools that exist but aren't covered by any capability
    # Group uncovered tools by tag prefix to create capability entries
    uncovered = supported - covered_tools
    if uncovered:
        # Group by first tag or tool name prefix
        groups: dict[str, list[str]] = {}
        for tool_name in sorted(uncovered):
            tool_def = tools_by_name.get(tool_name)
            tags = list(tool_def.tags) if tool_def and tool_def.tags else []
            # Use first meaningful tag as group key, or "general"
            group_key = "general"
            for tag in tags:
                if tag not in ("edge", "local", "mesh"):
                    group_key = tag
                    break
            if group_key == "general":
                # Try prefix-based grouping: "chrome_navigate" → "chrome"
                prefix = tool_name.split("_")[0] if "_" in tool_name else "general"
                group_key = prefix
            groups.setdefault(group_key, []).append(tool_name)

        from nexus.mesh.node_card import CapabilitySpec

        for group_key, tool_names in groups.items():
            cap_id = f"auto_{group_key}"
            descriptions = []
            for tn in tool_names:
                td = tools_by_name.get(tn)
                if td and td.description:
                    descriptions.append(f"{tn}: {td.description[:80]}")
            desc = f"自动发现的工具组 ({group_key}): " + "; ".join(descriptions[:5])
            kept_capabilities.append(CapabilitySpec(
                capability_id=cap_id,
                description=desc,
                tools=tool_names,
            ))
            state.add_event(
                "capability",
                f"Auto-discovered capability {cap_id} with {len(tool_names)} tools",
                level="info",
                tools=tool_names,
            )

    adjusted.capabilities = kept_capabilities
    return adjusted


def _browser_service(settings: NexusSettings, *, force_enabled: bool | None = None) -> BrowserService:
    configured_command = settings.browser_worker_command or default_browser_worker_command()
    enabled = settings.browser_enabled if force_enabled is None else force_enabled
    config = BrowserWorkerConfig(
        enabled=enabled,
        command=configured_command,
        workdir=settings.root_dir,
    )
    return BrowserService(config)


def _mesh_config(settings: NexusSettings, args: argparse.Namespace) -> dict[str, Any]:
    raw = settings.mesh_config()
    broker_host = args.broker_host or raw["broker_host"]
    broker_port = int(args.broker_port or raw["broker_port"])
    hub_api_host = getattr(args, "hub_api_host", "") or ""
    hub_api_port = int(getattr(args, "hub_api_port", 0) or 0)
    hub_api_bearer_token = (
        getattr(args, "hub_api_bearer_token", "")
        or os.getenv("NEXUS_HUB_BEARER_TOKEN")
        or os.getenv("NEXUS_BEARER_TOKEN")
        or ""
    ).strip()
    transport = str(args.mesh_transport or raw["transport"])
    mesh_username = args.mesh_username if args.mesh_username is not None else raw["username"]
    mesh_password = args.mesh_password if args.mesh_password is not None else raw["password"]
    tls_enabled = bool(raw["tls_enabled"] if args.tls_enabled is None else args.tls_enabled)
    node_card_path = str(args.node_card_path or raw["node_card_path"] or "")
    if not node_card_path:
        raise ValueError("node_card_path is required")
    return {
        "broker_host": broker_host,
        "broker_port": broker_port,
        "transport": transport,
        "websocket_path": raw["websocket_path"],
        "username": mesh_username,
        "password": mesh_password,
        "keepalive_seconds": int(raw["keepalive_seconds"]),
        "qos": int(raw["qos"]),
        "tls_enabled": tls_enabled,
        "tls_ca_path": raw["tls_ca_path"],
        "tls_cert_path": raw["tls_cert_path"],
        "tls_key_path": raw["tls_key_path"],
        "tls_insecure": bool(raw["tls_insecure"]),
        "node_card_path": node_card_path,
        **({"hub_api_host": hub_api_host} if hub_api_host else {}),
        **({"hub_api_port": hub_api_port} if hub_api_port else {}),
        **({"hub_api_bearer_token": hub_api_bearer_token} if hub_api_bearer_token else {}),
    }


class MacOSSidecarRuntime:
    """Compose the edge runtime plus the local HTTP status surface."""

    _OPEN_PATTERNS = (
        re.compile(r"^\s*(?:请你|请|帮我|麻烦你|你)?\s*(?:打开|启动|激活|切换到|切到|唤起)\s*(?P<app>.+?)\s*$", re.IGNORECASE),
        re.compile(r"^\s*(?:please\s+)?(?:open|launch|activate)\s+(?P<app>.+?)\s*$", re.IGNORECASE),
    )
    _APP_ALIASES = {
        "chrome": "Google Chrome",
        "google chrome": "Google Chrome",
        "chrome browser": "Google Chrome",
        "谷歌浏览器": "Google Chrome",
        "浏览器": "Google Chrome",
        "safari": "Safari",
        "finder": "Finder",
        "访达": "Finder",
        "terminal": "Terminal",
        "终端": "Terminal",
        "system settings": "System Settings",
        "系统设置": "System Settings",
    }
    _APP_SUFFIXES = (
        "浏览器",
        "应用程序",
        "应用",
        "程序",
        " browser",
        " app",
        " application",
    )
    _BROWSER_APPS = {"Google Chrome", "Safari"}
    _SITE_ALIASES = {
        "网易邮箱": "https://mail.163.com",
        "163邮箱": "https://mail.163.com",
        "mail.163.com": "https://mail.163.com",
        "gmail": "https://mail.google.com",
        "谷歌邮箱": "https://mail.google.com",
        "google mail": "https://mail.google.com",
        "outlook": "https://outlook.office.com/mail/",
        "outlook邮箱": "https://outlook.office.com/mail/",
    }
    _URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
    _MAC_CONTEXT_RE = re.compile(
        r"(?:^|\s)(?:你在|在|到)?\s*(?:这台|本机|当前|我的)?\s*(?:macbook\s*pro|macbook|mac)\s*上的",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        settings: NexusSettings,
        http_host: str,
        http_port: int,
        mesh_config: dict[str, Any],
        transport: MQTTTransport | Any | None = None,
        node_card: NodeCard | None = None,
        browser_enabled: bool | None = None,
    ) -> None:
        self._mesh_retry_interval_seconds = 10.0
        self._mesh_retry_task: asyncio.Task[None] | None = None
        self._journal_sync_task: asyncio.Task[None] | None = None
        self._journal_sync_interval_seconds = 60.0
        self._hub_probe_task: asyncio.Task[None] | None = None
        self._hub_probe_interval_seconds = 5.0
        self._settings = settings
        self._disable_risk_controls = settings.disable_risk_controls_for_testing
        self._mesh_config = mesh_config
        self._workspace_service = WorkspaceService([settings.root_dir, settings.vault_base_path])
        self._browser_service = _browser_service(settings, force_enabled=browser_enabled)
        self._tool_definitions = self._apply_testing_mode_to_tools(
            build_edge_tool_registry(
                workspace_service=self._workspace_service,
                browser_service=self._browser_service if self._browser_service.enabled else None,
            )
        )
        self._tool_executor = EdgeToolExecutor(self._tool_definitions)
        self._state = SidecarState(
            root_dir=settings.root_dir,
            http_host=http_host,
            http_port=http_port,
            tools=[_jsonable_tool(tool) for tool in self._tool_definitions],
            mesh_summary={
                "broker_host": mesh_config["broker_host"],
                "broker_port": mesh_config["broker_port"],
                "transport": mesh_config["transport"],
            },
            browser_enabled=self._browser_service.enabled,
            hub_api_host=self._hub_api_host,
            hub_api_port=self._hub_api_port,
        )
        self._approval_manager = ApprovalManager(self._state)
        self._runtime_tools = self._build_runtime_tools(self._tool_definitions)
        self._runtime_tools_by_name = {tool.name: tool for tool in self._runtime_tools}

        base_card = node_card or NodeCard.from_yaml_file(mesh_config["node_card_path"])
        reconciled = _reconcile_node_card(base_card, self._tool_definitions, self._state)
        self._transport = transport or MQTTTransport(
            reconciled.node_id,
            hostname=mesh_config["broker_host"],
            port=int(mesh_config["broker_port"]),
            username=mesh_config["username"],
            password=mesh_config["password"],
            transport=mesh_config["transport"],
            websocket_path=mesh_config["websocket_path"],
            keepalive=int(mesh_config["keepalive_seconds"]),
            qos=int(mesh_config["qos"]),
            tls_enabled=bool(mesh_config["tls_enabled"]),
            tls_ca_path=mesh_config["tls_ca_path"],
            tls_cert_path=mesh_config["tls_cert_path"],
            tls_key_path=mesh_config["tls_key_path"],
            tls_insecure=bool(mesh_config["tls_insecure"]),
        )
        self._state.node_card = reconciled.to_dict()

        # --- Edge Agent Runtime (local LLM-driven execution) ---
        data_root = Path(os.environ.get("NEXUS_DATA_ROOT", "")) if os.environ.get("NEXUS_DATA_ROOT") else settings.root_dir / "data"
        journal_dir = data_root / "edge_journal"
        self._journal = TaskJournal(journal_dir=journal_dir)
        provider_configs = _provider_configs_from_node_card(reconciled)
        self._edge_provider = build_edge_provider(provider_configs)
        if self._edge_provider:
            self._edge_runtime = EdgeAgentRuntime(
                provider=self._edge_provider,
                tools=list(self._runtime_tools),
                tools_policy=ToolsPolicy(
                    auto_approve_levels={
                        ToolRiskLevel.LOW,
                        ToolRiskLevel.MEDIUM,
                        ToolRiskLevel.HIGH,
                        ToolRiskLevel.CRITICAL,
                    }
                ),
                journal=self._journal,
            )
            logger.info(
                "Edge agent runtime ready (provider: %s)",
                self._edge_provider.get_provider().name,
            )
        else:
            self._edge_runtime = None
            logger.warning("No API providers configured — local command execution disabled")

        self._agent = ObservableEdgeNodeAgent(
            transport=self._transport,
            tool_executor=self._tool_executor,
            node_card=reconciled,
            heartbeat_interval_seconds=5.0,
            card_refresh_interval_seconds=15.0,
            load_provider=_predict_load,
            battery_level_provider=_battery_level_provider,
            approval_handler=self._auto_approve_request if self._disable_risk_controls else self._approval_manager.request,
            state=self._state,
            edge_runtime=self._edge_runtime,
            delegated_executor=self.execute_delegated_command,
        )
        if self._disable_risk_controls:
            self._state.add_event(
                "testing_mode",
                "Nexus risk controls disabled for testing; macOS system permissions still apply",
                level="warning",
            )
        self._refresh_hub_connectivity_state(emit_event=False)

    @property
    def state(self) -> SidecarState:
        return self._state

    @property
    def node_card(self) -> dict[str, Any] | None:
        return self._state.node_card

    @property
    def edge_runtime(self) -> EdgeAgentRuntime | None:
        return self._edge_runtime

    @property
    def edge_provider(self) -> "ProviderGateway | None":
        return self._edge_provider

    @property
    def journal(self) -> TaskJournal:
        return self._journal

    @property
    def approval_manager(self) -> ApprovalManager:
        return self._approval_manager

    def _apply_testing_mode_to_tools(self, tools: list[ToolDefinition]) -> list[ToolDefinition]:
        if not self._disable_risk_controls:
            return tools
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=dict(tool.parameters),
                handler=tool.handler,
                risk_level=tool.risk_level,
                requires_approval=False,
                tags=list(tool.tags),
            )
            for tool in tools
        ]

    def _build_runtime_tools(self, tools: list[ToolDefinition]) -> list[ToolDefinition]:
        return [self._wrap_runtime_tool(tool) for tool in tools]

    async def _auto_approve_request(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        context: ApprovalRequestContext,
    ) -> None:
        self._state.add_event(
            "approval_bypassed",
            f"{tool.name} auto-approved in testing mode",
            level="warning",
            tool_name=tool.name,
            source=context.source,
            arguments=arguments,
        )
        return None

    def _refresh_hub_connectivity_state(self, *, emit_event: bool = True) -> None:
        previous_state = self._state.hub_connectivity_state
        new_state = self._state.recompute_hub_connectivity_state()
        if not emit_event or new_state == previous_state:
            return

        if new_state == "connected":
            message = "Hub control plane is fully connected"
            level = "info"
        elif new_state == "broker_only":
            message = "Mesh broker connected, but Hub API/runtime is not fully ready"
            level = "warning"
        elif new_state == "reconnecting":
            message = "Reconnecting this Mac to the Hub"
            level = "warning"
        else:
            message = "Hub unavailable, local-only mode active"
            level = "warning"

        self._state.add_event(
            "hub_state",
            message,
            level=level,
            state=new_state,
            broker_connected=self._state.transport_connected,
            api_healthy=self._state.hub_api_healthy,
            runtime_ready=self._state.hub_runtime_ready,
        )

    def _apply_hub_probe_result(
        self,
        *,
        api_healthy: bool,
        runtime_ready: bool,
        hub_node_online: bool,
        error: str | None,
    ) -> None:
        previous_api = self._state.hub_api_healthy
        previous_runtime = self._state.hub_runtime_ready
        previous_hub_online = self._state.hub_node_online

        self._state.hub_api_healthy = api_healthy
        self._state.hub_runtime_ready = runtime_ready
        self._state.hub_node_online = hub_node_online
        self._state.hub_last_checked_at = time.time()
        self._state.hub_last_error = error

        if previous_api != api_healthy:
            self._state.add_event(
                "hub_probe",
                "Hub API reachable" if api_healthy else "Hub API unreachable",
                level="info" if api_healthy else "warning",
                api_host=self._hub_api_host,
                api_port=self._hub_api_port,
                error=error,
            )

        if previous_runtime != runtime_ready:
            self._state.add_event(
                "hub_probe",
                "Hub runtime ready" if runtime_ready else "Hub runtime not ready",
                level="info" if runtime_ready else "warning",
                hub_node_online=hub_node_online,
                error=error,
            )

        if previous_hub_online != hub_node_online and runtime_ready:
            self._state.add_event(
                "hub_probe",
                "Hub node is online in mesh registry" if hub_node_online else "Hub node is missing from mesh registry",
                level="info" if hub_node_online else "warning",
            )

        self._refresh_hub_connectivity_state()

    async def _probe_hub_once(self) -> None:
        timeout = aiohttp.ClientTimeout(total=3)
        api_healthy = False
        runtime_ready = False
        hub_node_online = False
        error: str | None = None

        base_url = f"http://{self._hub_api_host}:{self._hub_api_port}"
        headers = self._hub_api_headers()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers or None) as session:
                async with session.get(f"{base_url}/health") as response:
                    if response.status != 200:
                        raise RuntimeError(f"/health returned {response.status}")
                    payload = await response.json()
                    api_healthy = str(payload.get("status", "")).lower() == "ok"
                    if not api_healthy:
                        raise RuntimeError(f"Hub health reported {payload!r}")

                async with session.get(f"{base_url}/mesh/nodes") as response:
                    if response.status != 200:
                        raise RuntimeError(f"/mesh/nodes returned {response.status}")
                    payload = await response.json()
                    nodes = payload.get("nodes") or []
                    hub_nodes = [
                        node for node in nodes
                        if str(node.get("node_type", "")).lower() == "hub"
                    ]
                    hub_node_online = any(bool(node.get("online")) for node in hub_nodes)
                    runtime_ready = bool(hub_nodes) and hub_node_online
                    if not runtime_ready:
                        error = "Hub API is reachable, but mesh runtime is not ready"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)

        self._apply_hub_probe_result(
            api_healthy=api_healthy,
            runtime_ready=runtime_ready,
            hub_node_online=hub_node_online,
            error=error,
        )

    def _wrap_runtime_tool(self, tool: ToolDefinition) -> ToolDefinition:
        async def handler(**kwargs: Any) -> str:
            if tool.requires_approval:
                await self._approval_manager.request(
                    tool,
                    kwargs,
                    ApprovalRequestContext(
                        source="local_command",
                        timeout_seconds=300.0,
                        metadata={"mode": "local_command"},
                    ),
                )

            result = await self._tool_executor.execute(tool.name, kwargs)
            if not result.success:
                raise RuntimeError(result.error or f"Local tool failed: {tool.name}")
            return result.output

        return ToolDefinition(
            name=tool.name,
            description=tool.description,
            parameters=dict(tool.parameters),
            handler=handler,
            risk_level=tool.risk_level,
            requires_approval=tool.requires_approval,
            tags=list(tool.tags),
        )

    async def execute_local_command(
        self,
        task: str,
        *,
        system_prompt: str | None = None,
        provider_name: str | None = None,
    ) -> LocalRunResult:
        fast_path = await self._maybe_execute_local_fast_path(task)
        if fast_path is not None:
            return fast_path
        if self._edge_runtime is None:
            raise RuntimeError("No LLM provider configured — local command execution is unavailable")
        return await self._edge_runtime.run_local(
            task,
            system_prompt=system_prompt,
            provider_name=provider_name,
        )

    async def execute_delegated_command(
        self,
        task: str,
        constraints: dict[str, Any] | None = None,
    ) -> LocalRunResult:
        fast_path = await self._maybe_execute_local_fast_path(task)
        if fast_path is not None:
            return fast_path
        if self._edge_runtime is None:
            raise RuntimeError("No EdgeAgentRuntime configured — agent_loop execution unavailable")
        return await self._edge_runtime.run_delegated(
            task,
            constraints=constraints if constraints else None,
        )

    # Keywords indicating the task requires page interaction, not just opening a URL.
    _INTERACTION_KEYWORDS = re.compile(
        r"(?:查看|阅读|读取|读|看|浏览|点击|搜索|获取|提取|下载|列出|显示|操作|进入|打开.*并|"
        r"收件箱|邮件列表|邮件内容|最新|内容|正文|标题|发件人|"
        r"read|view|check|click|search|list|browse|extract|get|fetch|inbox|content)",
        re.IGNORECASE,
    )

    async def _maybe_execute_local_fast_path(self, task: str) -> LocalRunResult | None:
        tool = self._runtime_tools_by_name.get("run_applescript")
        if tool is None:
            return None

        browser_target = self._match_browser_target_task(task)
        if browser_target is not None:
            # Only use fast-path for simple "open X" tasks.
            # If the task requires page interaction (reading emails, clicking, etc.),
            # let the full agent loop handle it with chrome_* tools.
            if not self._INTERACTION_KEYWORDS.search(task):
                browser_app, target_url = browser_target
                return await self._run_browser_handoff_fast_path(
                    task=task,
                    tool=tool,
                    browser_app=browser_app,
                    target_url=target_url,
                )

        app_name = self._match_open_app_task(task)
        if not app_name:
            return None

        started = time.perf_counter()
        run_id = f"fast-{uuid.uuid4().hex[:12]}"
        script = self._activation_script_for_app(app_name)
        try:
            output = await tool.handler(script=script)
            duration_ms = (time.perf_counter() - started) * 1000
            return LocalRunResult(
                run_id=run_id,
                task=task,
                success=True,
                output=f"已在这台 Mac 上打开 {app_name}。\n\n工具输出：{output}",
                events=[
                    RunEvent(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id,
                        event_type="tool_result",
                        data={"tool": "run_applescript", "success": True, "fast_path": True},
                    )
                ],
                duration_ms=duration_ms,
                model="fast-path",
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            return LocalRunResult(
                run_id=run_id,
                task=task,
                success=False,
                output="",
                events=[
                    RunEvent(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id,
                        event_type="tool_result",
                        data={"tool": "run_applescript", "success": False, "fast_path": True, "error": str(exc)},
                    )
                ],
                error=str(exc),
                duration_ms=duration_ms,
                model="fast-path",
            )

    def _match_open_app_task(self, task: str) -> str | None:
        normalized = task.strip().strip("`\"' ")
        if not normalized:
            return None
        for pattern in self._OPEN_PATTERNS:
            match = pattern.match(normalized)
            if not match:
                continue
            app = match.group("app").strip().strip("`\"' ")
            key = self._normalize_requested_app_key(app)
            return self._APP_ALIASES.get(key)
        return None

    def _match_browser_target_task(self, task: str) -> tuple[str, str] | None:
        browser_app = self._detect_browser_app(task)
        if browser_app is None:
            return None

        url_match = self._URL_RE.search(task)
        if url_match:
            return browser_app, url_match.group(1)

        normalized = task.strip()
        for alias, url in self._SITE_ALIASES.items():
            if alias.lower() in normalized.lower():
                return browser_app, url
        return None

    def _detect_browser_app(self, task: str) -> str | None:
        normalized = self._normalize_requested_app_key(task)
        for alias_key, app_name in self._APP_ALIASES.items():
            if app_name not in self._BROWSER_APPS:
                continue
            alias_normalized = self._normalize_requested_app_key(alias_key)
            if alias_normalized and alias_normalized in normalized:
                return app_name
        return None

    def _normalize_requested_app_key(self, raw: str) -> str:
        value = raw.strip().strip("`\"' ")
        value = self._MAC_CONTEXT_RE.sub(" ", value)
        if "上的" in value:
            value = value.rsplit("上的", 1)[-1]
        value = re.sub(r"\s+", " ", value).strip().lower()
        changed = True
        while changed and value:
            changed = False
            for suffix in self._APP_SUFFIXES:
                if value.endswith(suffix):
                    value = value[: -len(suffix)].strip()
                    changed = True
        return re.sub(r"\s+", " ", value).strip()

    async def _run_browser_handoff_fast_path(
        self,
        *,
        task: str,
        tool: ToolDefinition,
        browser_app: str,
        target_url: str,
    ) -> LocalRunResult:
        started = time.perf_counter()
        run_id = f"fast-{uuid.uuid4().hex[:12]}"
        script = self._browser_navigation_script(browser_app, target_url)
        try:
            output = await tool.handler(script=script)
            duration_ms = (time.perf_counter() - started) * 1000
            return LocalRunResult(
                run_id=run_id,
                task=task,
                success=True,
                output=f"已在这台 Mac 的 {browser_app} 中打开 {target_url}。\n\n工具输出：{output}",
                events=[
                    RunEvent(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id,
                        event_type="tool_result",
                        data={
                            "tool": "run_applescript",
                            "success": True,
                            "fast_path": True,
                            "browser_app": browser_app,
                            "target_url": target_url,
                        },
                    )
                ],
                duration_ms=duration_ms,
                model="fast-path",
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            return LocalRunResult(
                run_id=run_id,
                task=task,
                success=False,
                output="",
                events=[
                    RunEvent(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id,
                        event_type="tool_result",
                        data={
                            "tool": "run_applescript",
                            "success": False,
                            "fast_path": True,
                            "browser_app": browser_app,
                            "target_url": target_url,
                            "error": str(exc),
                        },
                    )
                ],
                error=str(exc),
                duration_ms=duration_ms,
                model="fast-path",
            )

    def _activation_script_for_app(self, app_name: str) -> str:
        if app_name == "Google Chrome":
            return (
                'tell application "Google Chrome"\n'
                "activate\n"
                "if (count of windows) = 0 then make new window\n"
                "end tell\n"
                'try\n'
                'tell application "System Events" to tell process "Google Chrome" to set frontmost to true\n'
                "end try"
            )
        if app_name == "Safari":
            return (
                'tell application "Safari"\n'
                "activate\n"
                "if (count of windows) = 0 then make new document\n"
                "end tell\n"
                'try\n'
                'tell application "System Events" to tell process "Safari" to set frontmost to true\n'
                "end try"
            )
        return f'tell application "{app_name}" to activate'

    def _browser_navigation_script(self, browser_app: str, target_url: str) -> str:
        safe_url = target_url.replace('"', '\\"')
        if browser_app == "Google Chrome":
            return (
                'tell application "Google Chrome"\n'
                "activate\n"
                "if (count of windows) = 0 then make new window\n"
                f'set URL of active tab of front window to "{safe_url}"\n'
                "end tell\n"
                'try\n'
                'tell application "System Events" to tell process "Google Chrome" to set frontmost to true\n'
                "end try"
            )
        return (
            'tell application "Safari"\n'
            "activate\n"
            "if (count of windows) = 0 then make new document\n"
            f'set URL of front document to "{safe_url}"\n'
            "end tell\n"
            'try\n'
            'tell application "System Events" to tell process "Safari" to set frontmost to true\n'
            "end try"
        )

    @property
    def _hub_api_host(self) -> str:
        return str(self._mesh_config.get("hub_api_host") or self._mesh_config["broker_host"])

    @property
    def _hub_api_port(self) -> int:
        return int(self._mesh_config.get("hub_api_port") or 8000)

    @property
    def _hub_api_bearer_token(self) -> str:
        return str(self._mesh_config.get("hub_api_bearer_token") or "").strip()

    def _hub_api_headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        token = self._hub_api_bearer_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def startup(self) -> None:
        self._state.add_event("startup", "Starting macOS sidecar")
        try:
            await self._agent.start()
        except Exception as exc:
            self._state.transport_connected = False
            self._state.hub_reconnecting = True
            self._state.set_phase("running", error=f"Hub unavailable: {exc}")
            self._state.add_event(
                "transport",
                "Hub unavailable, running local only",
                level="warning",
                error=str(exc),
                broker_host=self._mesh_config["broker_host"],
                broker_port=self._mesh_config["broker_port"],
            )
            self._refresh_hub_connectivity_state()
            self._mesh_retry_task = asyncio.create_task(
                self._retry_mesh_connect_loop(),
                name="macos-sidecar-mesh-retry",
            )
        else:
            self._state.hub_reconnecting = False
            self._refresh_hub_connectivity_state()

        self._hub_probe_task = asyncio.create_task(
            self._hub_probe_loop(),
            name="macos-sidecar-hub-probe",
        )

        # Start journal sync loop
        self._journal_sync_task = asyncio.create_task(
            self._journal_sync_loop(),
            name="macos-sidecar-journal-sync",
        )

    async def shutdown(self) -> None:
        self._state.add_event("shutdown", "Stopping macOS sidecar")
        if self._hub_probe_task is not None:
            self._hub_probe_task.cancel()
            try:
                await self._hub_probe_task
            except asyncio.CancelledError:
                pass
            self._hub_probe_task = None
        if self._journal_sync_task is not None:
            self._journal_sync_task.cancel()
            try:
                await self._journal_sync_task
            except asyncio.CancelledError:
                pass
            self._journal_sync_task = None
        if self._mesh_retry_task is not None:
            self._mesh_retry_task.cancel()
            try:
                await self._mesh_retry_task
            except asyncio.CancelledError:
                pass
            self._mesh_retry_task = None
        # Final sync attempt before stopping
        if self._journal.unsynced_entries():
            node_id = (self._state.node_card or {}).get("node_id", "unknown")
            await self._journal.sync_to_hub(
                hub_host=self._hub_api_host,
                hub_port=self._hub_api_port,
                node_id=node_id,
                bearer_token=self._hub_api_bearer_token,
            )
        await self._agent.stop()
        await self._browser_service.aclose()

    async def _journal_sync_loop(self) -> None:
        """Periodically sync unsynced journal entries to the Hub."""
        try:
            while True:
                await asyncio.sleep(self._journal_sync_interval_seconds)
                if not self._journal.unsynced_entries():
                    continue
                node_id = (self._state.node_card or {}).get("node_id", "unknown")
                synced = await self._journal.sync_to_hub(
                    hub_host=self._hub_api_host,
                    hub_port=self._hub_api_port,
                    node_id=node_id,
                    bearer_token=self._hub_api_bearer_token,
                )
                if synced > 0:
                    self._state.add_event(
                        "journal_sync",
                        f"Synced {synced} journal entries to Hub",
                    )
        except asyncio.CancelledError:
            raise

    async def _hub_probe_loop(self) -> None:
        try:
            while True:
                await self._probe_hub_once()
                await asyncio.sleep(self._hub_probe_interval_seconds)
        except asyncio.CancelledError:
            raise

    async def _retry_mesh_connect_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._mesh_retry_interval_seconds)
                if self._state.transport_connected:
                    self._state.hub_reconnecting = False
                    self._refresh_hub_connectivity_state()
                    return
                self._state.add_event(
                    "transport",
                    "Retrying hub connection",
                    level="info",
                    broker_host=self._mesh_config["broker_host"],
                    broker_port=self._mesh_config["broker_port"],
                )
                try:
                    await self._agent.start()
                except Exception as exc:
                    self._state.transport_connected = False
                    self._state.hub_reconnecting = True
                    self._state.set_phase("running", error=f"Hub unavailable: {exc}")
                    self._state.add_event(
                        "transport",
                        "Hub still unavailable",
                        level="warning",
                        error=str(exc),
                        broker_host=self._mesh_config["broker_host"],
                        broker_port=self._mesh_config["broker_port"],
                    )
                    self._refresh_hub_connectivity_state()
                    continue
                self._state.hub_reconnecting = False
                self._refresh_hub_connectivity_state()
                return
        except asyncio.CancelledError:
            raise

    def app(self) -> FastAPI:
        runtime = self

        @asynccontextmanager
        async def lifespan(_: FastAPI):
            await runtime.startup()
            try:
                yield
            finally:
                await runtime.shutdown()

        app = FastAPI(title="Nexus macOS Sidecar", version="0.1.0", lifespan=lifespan)

        # Allow Desktop WKWebView (file://) cross-origin requests
        # file:// origins send "null" as Origin header, must use allow_origin_regex
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_origin_regex=r".*",
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/health")
        async def health() -> dict[str, Any]:
            status = "ok" if runtime.state.phase == "running" else "degraded"
            return {
                "status": status,
                "phase": runtime.state.phase,
                "transport_connected": runtime.state.transport_connected,
                "hub_connectivity_state": runtime.state.hub_connectivity_state,
                "node_id": (runtime.state.node_card or {}).get("node_id"),
            }

        @app.get("/status")
        async def status() -> dict[str, Any]:
            return runtime.state.snapshot()

        @app.get("/events")
        async def events() -> dict[str, Any]:
            return {"events": [event.to_dict() for event in runtime.state.recent_events]}

        @app.get("/tools")
        async def tools() -> dict[str, Any]:
            return {"tools": runtime.state.tools}

        @app.get("/node-card")
        async def node_card() -> dict[str, Any]:
            return {"node_card": runtime.node_card}

        @app.get("/providers")
        async def providers() -> dict[str, Any]:
            """List all configured LLM providers with health status."""
            if runtime.edge_provider is None:
                return {"providers": []}
            result = []
            for p in runtime.edge_provider.list_providers():
                health = runtime.edge_provider.get_health_snapshot(p.name)
                result.append({
                    "name": p.name,
                    "model": p.model,
                    "provider_type": p.provider or p.name,
                    "status": health.get("status", "unknown"),
                })
            return {"providers": result}

        @app.get("/permissions")
        async def permissions() -> dict[str, Any]:
            """Check macOS permissions for the sidecar process (the one that actually runs automation)."""
            return {"permissions": await _check_sidecar_permissions()}

        @app.get("/approvals")
        async def approvals() -> dict[str, Any]:
            return {"approvals": runtime.approval_manager.snapshot()}

        @app.post("/approvals/{approval_id}/approve")
        async def approve_approval(approval_id: str, request: ApprovalActionRequest) -> dict[str, Any]:
            try:
                approval = await runtime.approval_manager.resolve(
                    approval_id,
                    approved=True,
                    comment=request.comment,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"Unknown approval: {approval_id}") from exc
            return {"approval": approval.to_dict()}

        @app.post("/approvals/{approval_id}/reject")
        async def reject_approval(approval_id: str, request: ApprovalActionRequest) -> dict[str, Any]:
            try:
                approval = await runtime.approval_manager.resolve(
                    approval_id,
                    approved=False,
                    comment=request.comment,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"Unknown approval: {approval_id}") from exc
            return {"approval": approval.to_dict()}

        @app.post("/local-command")
        async def local_command(request: LocalCommandRequest):
            if runtime.edge_runtime is None:
                raise HTTPException(
                    status_code=503,
                    detail="No LLM provider configured — local command execution is unavailable",
                )

            # Use SSE to avoid WKWebView fetch timeout on long-running tasks.
            # Send periodic keep-alive while the agent loop runs, then the result.
            from fastapi.responses import StreamingResponse
            import json as _json

            async def sse_stream():
                # Ack
                yield f"data: {_json.dumps({'type': 'status', 'content': 'Sidecar executing...'})}\n\n"

                task_done = asyncio.Event()
                result_holder: list[Any] = []

                local_task_id = f"local-{uuid.uuid4().hex[:8]}"

                # Task Log — record local command received
                runtime.state.task_log.append(
                    task_id=local_task_id,
                    phase="received",
                    source="desktop",
                    node=runtime.state.mesh_summary.get("node_id", "mac"),
                    message=f"📥 本地任务: {request.task[:200]}",
                )

                async def run_task():
                    try:
                        r = await runtime.execute_local_command(
                            request.task,
                            system_prompt=request.system_prompt,
                            provider_name=request.provider_name,
                        )
                        result_holder.append(r)
                    except Exception as exc:
                        result_holder.append(exc)
                    finally:
                        task_done.set()

                task = asyncio.create_task(run_task())

                # Task Log — record executing
                runtime.state.task_log.append(
                    task_id=local_task_id,
                    phase="executing",
                    source="desktop",
                    node=runtime.state.mesh_summary.get("node_id", "mac"),
                    message="⚙️ 开始执行...",
                )

                # Send keep-alive every 10s while task is running
                while not task_done.is_set():
                    try:
                        await asyncio.wait_for(task_done.wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        yield f"data: {_json.dumps({'type': 'status', 'content': 'Still working...'})}\n\n"

                if not result_holder or isinstance(result_holder[0], Exception):
                    err_msg = str(result_holder[0]) if result_holder else "Unknown error"
                    runtime.state.task_log.append(
                        task_id=local_task_id,
                        phase="failed",
                        source="desktop",
                        node=runtime.state.mesh_summary.get("node_id", "mac"),
                        message=f"❌ 失败: {err_msg[:200]}",
                    )
                    yield f"data: {_json.dumps({'type': 'error', 'content': err_msg})}\n\n"
                else:
                    result = result_holder[0]
                    runtime.state.add_event(
                        "local_command",
                        f"Local command {'succeeded' if result.success else 'failed'}: {request.task[:80]}",
                        level="info" if result.success else "error",
                        run_id=result.run_id,
                        duration_ms=result.duration_ms,
                    )
                    runtime.state.task_log.append(
                        task_id=local_task_id,
                        phase="completed" if result.success else "failed",
                        source="desktop",
                        node=runtime.state.mesh_summary.get("node_id", "mac"),
                        message=f"{'✅ 完成' if result.success else '❌ 失败'}: {request.task[:100]}",
                        duration_ms=result.duration_ms,
                        output_preview=result.output[:300] if result.output else "",
                    )
                    yield f"data: {_json.dumps({'type': 'result', 'result': result.to_dict()})}\n\n"

            return StreamingResponse(sse_stream(), media_type="text/event-stream")

        @app.get("/journal")
        async def journal_status() -> dict[str, Any]:
            unsynced = runtime.journal.unsynced_entries()
            return {
                "unsynced_count": len(unsynced),
                "entries": [e.to_dict() for e in unsynced[:20]],
            }

        @app.get("/task-log")
        async def task_log_api(request: Request):
            """Return recent task log entries for the Task Log window."""
            try:
                task_id = request.query_params.get("task_id")
                limit = int(request.query_params.get("limit", "100"))
                if task_id:
                    entries = runtime.state.task_log.by_task(task_id)
                else:
                    entries = runtime.state.task_log.recent(limit=limit)
                return {"entries": entries, "count": len(entries)}
            except Exception as e:
                logger.exception("task-log endpoint error")
                return {"entries": [], "count": 0, "error": str(e)}

        # --- Editor AI endpoints (inline completion + transform) ---

        @app.post("/editor/complete")
        async def editor_complete(req: EditorCompleteRequest):
            """Inline completion: return a short continuation suggestion via SSE."""
            from fastapi.responses import StreamingResponse
            import json as _json

            if runtime.edge_provider is None:
                raise HTTPException(status_code=503, detail="No LLM provider configured")

            context_before = req.context_before[-2000:] if req.context_before else ""
            context_after = req.context_after[:500] if req.context_after else ""

            if len(context_before.strip()) < 5:
                # 上下文太短，不值得补全
                async def empty():
                    yield f"data: {_json.dumps({'type': 'done'})}\n\n"
                return StreamingResponse(empty(), media_type="text/event-stream")

            system_prompt = (
                "You are a writing assistant. Complete the user's text naturally.\n"
                "Rules:\n"
                "- Continue in the same language as the user's text\n"
                "- Match the user's writing style and tone\n"
                "- Keep the completion concise (1-2 sentences max)\n"
                "- Do NOT repeat what the user has already written\n"
                "- Output ONLY the continuation text, no explanations, no quotes\n"
                "- If the text appears complete, output nothing"
            )

            user_text = context_before
            if context_after.strip():
                user_text += f"\n[CURSOR_HERE — complete the text between what's before and after]\n{context_after}"
            else:
                user_text += "\n[CURSOR_HERE — continue from here]"

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ]

            async def stream():
                try:
                    result = await runtime.edge_provider.chat_completion(
                        messages=messages,
                        temperature=0.3,
                        max_tokens=min(req.max_tokens, 300),
                        extra_body={"thinking": {"type": "disabled"}},
                    )
                    text = result.get("message", {}).get("content", "").strip()
                    fallback = "模型返回空内容，请重试或切换模型。"
                    if text and text != fallback:
                        # 去掉模型可能添加的引号或前缀
                        for prefix in ["[CURSOR_HERE]", "[CURSOR_HERE —", "继续：", "Continuation:"]:
                            if text.startswith(prefix):
                                text = text[len(prefix):].strip()
                        if text:
                            yield f"data: {_json.dumps({'type': 'suggestion', 'text': text})}\n\n"
                    yield f"data: {_json.dumps({'type': 'done'})}\n\n"
                except Exception as exc:
                    logger.warning("Editor completion failed: %s", exc)
                    yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.post("/editor/transform")
        async def editor_transform(req: EditorTransformRequest):
            """Transform selected text (translate, polish, expand, etc.) via SSE."""
            from fastapi.responses import StreamingResponse
            import json as _json

            if runtime.edge_provider is None:
                raise HTTPException(status_code=503, detail="No LLM provider configured")

            if not req.selected_text.strip():
                raise HTTPException(status_code=400, detail="No text selected")

            # 构建 system prompt
            prompts = {
                "translate": (
                    "You are a professional translator.\n"
                    "Translate the given text between Chinese and English.\n"
                    f"Target language: {req.target_language or 'auto-detect and translate to the other language'}.\n"
                    "Rules:\n"
                    "- Preserve formatting (markdown, line breaks)\n"
                    "- Preserve technical terms appropriately\n"
                    "- Output ONLY the translated text, no explanations"
                ),
                "polish": (
                    "You are a professional editor.\n"
                    "Polish the given text to improve clarity, grammar, and readability.\n"
                    "Rules:\n"
                    "- Maintain the original language\n"
                    "- Preserve the original meaning\n"
                    "- Fix grammar and punctuation\n"
                    "- Improve sentence flow\n"
                    "- Output ONLY the polished text, no explanations"
                ),
                "expand": (
                    "You are a writing assistant.\n"
                    "Expand the given text with more detail and elaboration.\n"
                    "Rules:\n"
                    "- Maintain the original language and style\n"
                    "- Add relevant details, examples, or explanations\n"
                    "- Keep the expansion natural and coherent\n"
                    "- Output ONLY the expanded text, no explanations"
                ),
                "condense": (
                    "You are a professional editor.\n"
                    "Condense the given text to be more concise while preserving key information.\n"
                    "Rules:\n"
                    "- Maintain the original language\n"
                    "- Remove redundancy and filler\n"
                    "- Keep essential meaning intact\n"
                    "- Output ONLY the condensed text, no explanations"
                ),
                "rewrite": (
                    "You are a writing assistant.\n"
                    "Rewrite the given text in a different style while preserving the meaning.\n"
                    "Rules:\n"
                    "- Maintain the original language\n"
                    "- Use a fresh perspective or phrasing\n"
                    "- Output ONLY the rewritten text, no explanations"
                ),
            }

            system = prompts.get(req.action)
            if not system:
                raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

            # 添加上下文
            user_content = req.selected_text
            if req.context_before or req.context_after:
                surrounding = ""
                if req.context_before:
                    surrounding += f"[Context before]: {req.context_before[-500:]}\n\n"
                if req.context_after:
                    surrounding += f"[Context after]: {req.context_after[:500]}\n\n"
                user_content = f"{surrounding}[Text to {req.action}]:\n{req.selected_text}"

            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]

            selected_length = max(len(req.selected_text.strip()), 1)
            max_tokens_by_action = {
                "translate": min(max(selected_length * 4, 64), 192),
                "polish": min(max(selected_length * 3, 96), 320),
                "expand": min(max(selected_length * 4, 160), 512),
                "condense": min(max(selected_length * 2, 96), 256),
                "rewrite": min(max(selected_length * 3, 128), 384),
            }
            max_tokens = max_tokens_by_action.get(req.action, 256)

            async def stream():
                try:
                    result = await runtime.edge_provider.chat_completion(
                        messages=messages,
                        temperature=0.3,
                        max_tokens=max_tokens,
                        extra_body={"thinking": {"type": "disabled"}},
                    )
                    text = result.get("message", {}).get("content", "").strip()
                    if text:
                        yield f"data: {_json.dumps({'type': 'transform', 'text': text})}\n\n"
                    yield f"data: {_json.dumps({'type': 'done'})}\n\n"
                except Exception as exc:
                    logger.warning("Editor transform failed: %s", exc)
                    yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")

        # --- Hub Vault proxy (avoids CORS issues for WebView) ---
        hub_base = f"http://{runtime._hub_api_host}:{runtime._hub_api_port}"

        @app.api_route("/vault/{rest_of_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def vault_proxy(rest_of_path: str, request: Request):
            """Proxy vault requests to Hub so WebView stays same-origin."""
            target = f"{hub_base}/vault/{rest_of_path}"
            if request.url.query:
                target += f"?{request.url.query}"
            timeout_cfg = aiohttp.ClientTimeout(total=10)
            try:
                body = await request.body()
                headers = runtime._hub_api_headers(
                    content_type=request.headers.get("content-type", "application/json")
                )
                async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                    async with session.request(
                        request.method, target, data=body if body else None, headers=headers
                    ) as resp:
                        data = await resp.read()
                        from fastapi.responses import Response
                        return Response(
                            content=data,
                            status_code=resp.status,
                            media_type=resp.content_type,
                        )
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Hub unreachable: {e}")

        # --- Hub SSE proxy for /desktop/message (chat via Hub) ---
        @app.post("/hub/desktop/message")
        async def hub_message_proxy(request: Request):
            """Proxy desktop message SSE to Hub, avoiding CORS."""
            from fastapi.responses import StreamingResponse
            target = f"{hub_base}/desktop/message"
            body = await request.body()
            timeout_cfg = aiohttp.ClientTimeout(total=300)
            try:
                session = aiohttp.ClientSession(timeout=timeout_cfg)
                resp = await session.post(
                    target,
                    data=body,
                    headers=runtime._hub_api_headers(content_type="application/json"),
                )

                async def stream():
                    try:
                        async for chunk in resp.content.iter_any():
                            yield chunk
                    finally:
                        resp.close()
                        await session.close()

                return StreamingResponse(
                    stream(),
                    status_code=resp.status,
                    media_type=resp.content_type,
                )
            except Exception as e:
                await session.close()
                raise HTTPException(status_code=502, detail=f"Hub unreachable: {e}")

        # --- Serve Desktop web UI from bundled dist or adjacent project ---
        web_ui_dir = _find_web_ui_dir(runtime._settings.root_dir)
        if web_ui_dir:
            logger.info("Serving Desktop web UI from %s", web_ui_dir)
            # Mount static files at root so relative paths (./assets/...) work
            # regardless of whether the page is loaded from /ui or /ui/
            app.mount("/", StaticFiles(directory=str(web_ui_dir), html=True), name="web-ui")
        else:
            logger.info("No Desktop web UI dist found — static UI disabled")

        return app


def _find_web_ui_dir(root_dir: Path) -> Path | None:
    """Locate the Desktop web UI dist directory."""
    candidates = [
        root_dir.parent / "web-ui",              # Bundled app: Resources/web-ui/
        root_dir / "apps" / "desktop" / "dist",   # Dev: Nexus/apps/desktop/dist/
        root_dir / "web-ui",                       # Alt layout
    ]
    for d in candidates:
        if (d / "index.html").is_file():
            return d
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Nexus macOS edge sidecar.")
    parser.add_argument("--root", type=Path, default=None, help="Path to the Nexus workspace root.")
    parser.add_argument("--http-host", default="127.0.0.1", help="Local HTTP bind host.")
    parser.add_argument("--http-port", type=int, default=8765, help="Local HTTP bind port.")
    parser.add_argument("--node-card-path", default="", help="Path to the node card YAML.")
    parser.add_argument("--broker-host", default="", help="MQTT broker hostname override.")
    parser.add_argument("--broker-port", type=int, default=0, help="MQTT broker port override.")
    parser.add_argument(
        "--mesh-transport",
        default="",
        choices=["", "tcp", "websockets"],
        help="MQTT transport override.",
    )
    parser.add_argument("--hub-api-host", default="", help="Hub API hostname override.")
    parser.add_argument("--hub-api-port", type=int, default=0, help="Hub API port override.")
    parser.add_argument("--hub-api-bearer-token", default="", help="Hub API bearer token override.")
    parser.add_argument("--mesh-username", default=None, help="MQTT username override.")
    parser.add_argument("--mesh-password", default=None, help="MQTT password override.")
    parser.add_argument("--tls-enabled", action="store_true", default=None, help="Enable MQTT TLS.")
    parser.add_argument("--log-level", default="INFO", help="Logging level.")
    return parser.parse_args(argv)


def build_runtime_from_args(args: argparse.Namespace) -> MacOSSidecarRuntime:
    settings = load_nexus_settings(args.root)
    mesh_config = _mesh_config(settings, args)
    return MacOSSidecarRuntime(
        settings=settings,
        http_host=args.http_host,
        http_port=int(args.http_port),
        mesh_config=mesh_config,
    )


async def _serve(args: argparse.Namespace) -> None:
    runtime = build_runtime_from_args(args)
    app = runtime.app()
    config = uvicorn.Config(
        app,
        host=args.http_host,
        port=int(args.http_port),
        log_level=str(args.log_level).lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve(args))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
