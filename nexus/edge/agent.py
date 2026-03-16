"""Runtime for an edge node participating in the mesh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nexus.agent.types import ToolDefinition, ToolResult
from nexus.mesh.node_card import CapabilitySpec, NodeCard
from nexus.mesh.task_protocol import (
    TaskAssignment,
    TaskExecutionResult,
    TaskStepState,
    task_result_topic,
    task_status_topic,
)
from nexus.mesh.transport import MeshMessage, MeshTransport, MessageType

from .local_runtime import EdgeAgentRuntime
from .tools import EdgeToolExecutor

logger = logging.getLogger(__name__)

LoadProvider = Callable[[], float | Awaitable[float]]
BatteryProvider = Callable[[], float | None | Awaitable[float | None]]


@dataclass(slots=True)
class ApprovalRequestContext:
    source: str
    source_node: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


ApprovalHandler = Callable[[ToolDefinition, dict[str, Any], ApprovalRequestContext], Awaitable[None]]


class EdgeNodeAgent:
    """Registers a node card, emits heartbeats, and executes structured tool calls."""

    def __init__(
        self,
        *,
        transport: MeshTransport,
        tool_executor: EdgeToolExecutor,
        node_card: NodeCard | None = None,
        node_card_path: str | None = None,
        heartbeat_interval_seconds: float = 30.0,
        card_refresh_interval_seconds: float = 60.0,
        load_provider: LoadProvider | None = None,
        battery_level_provider: BatteryProvider | None = None,
        approval_handler: ApprovalHandler | None = None,
        edge_runtime: EdgeAgentRuntime | None = None,
    ) -> None:
        if node_card is None and not node_card_path:
            raise ValueError("EdgeNodeAgent requires node_card or node_card_path")
        self._transport = transport
        self._tool_executor = tool_executor
        self._node_card = node_card
        self._node_card_path = node_card_path
        self._heartbeat_interval = heartbeat_interval_seconds
        self._card_refresh_interval = max(
            float(card_refresh_interval_seconds or 0.0),
            float(heartbeat_interval_seconds or 0.0),
        )
        self._load_provider = load_provider
        self._battery_level_provider = battery_level_provider
        self._approval_handler = approval_handler
        self._edge_runtime = edge_runtime

        self._heartbeat_task: asyncio.Task[None] | None = None
        self._subscribed_topics: set[str] = set()
        self._active_executions = 0
        self._started = False
        self._last_card_publish_at = 0.0

    @property
    def node_id(self) -> str:
        return self._resolved_node_card().node_id

    async def start(self) -> None:
        if self._started:
            return

        await self._transport.connect()
        card = self._validated_node_card()
        self._node_card = card

        await self._subscribe(f"nexus/rpc/{card.node_id}/+")
        await self._subscribe("nexus/tasks/+/assign")
        await self._publish_node_card(card)

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"edge-heartbeat-{card.node_id}",
        )
        self._started = True

    async def stop(self) -> None:
        if not self._started and not self._transport.connected:
            return

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Edge heartbeat task exited with error during shutdown", exc_info=True)
            self._heartbeat_task = None

        if self._transport.connected:
            try:
                await self._publish_offline()
            except Exception:
                logger.warning("Failed to publish edge offline event during shutdown", exc_info=True)

        for topic in list(self._subscribed_topics):
            try:
                await self._transport.unsubscribe(topic)
            except Exception:
                logger.warning("Failed to unsubscribe edge topic=%s", topic, exc_info=True)
            finally:
                self._subscribed_topics.discard(topic)

        await self._transport.disconnect()
        self._started = False

    async def _subscribe(self, topic_pattern: str) -> None:
        callback = self._on_rpc_request if topic_pattern.startswith("nexus/rpc/") else self._on_task_assigned
        await self._transport.subscribe(topic_pattern, callback)
        self._subscribed_topics.add(topic_pattern)

    async def _publish_node_card(self, card: NodeCard) -> None:
        topic = f"nexus/nodes/{card.node_id}/card"
        message = self._transport.make_message(
            MessageType.NODE_REGISTER,
            topic,
            card.to_dict(),
        )
        await self._transport.publish(topic, message)
        self._last_card_publish_at = asyncio.get_running_loop().time()

    async def _publish_offline(self) -> None:
        topic = f"nexus/nodes/{self.node_id}/offline"
        message = self._transport.make_message(
            MessageType.NODE_OFFLINE,
            topic,
            {"node_id": self.node_id},
        )
        await self._transport.publish(topic, message)

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                try:
                    now = asyncio.get_running_loop().time()
                    if (now - self._last_card_publish_at) >= self._card_refresh_interval:
                        await self._publish_node_card(self._resolved_node_card())
                    topic = f"nexus/nodes/{self.node_id}/heartbeat"
                    message = self._transport.make_message(
                        MessageType.NODE_HEARTBEAT,
                        topic,
                        {
                            "current_load": await self._current_load(),
                            "active_tasks": self._active_executions,
                            "battery_level": await self._battery_level(),
                        },
                    )
                    await self._transport.publish(topic, message)
                    await asyncio.sleep(self._heartbeat_interval)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Edge heartbeat failed for node=%s; recovering transport", self.node_id, exc_info=True)
                    await self._recover_transport(exc)
        except asyncio.CancelledError:
            raise

    async def _recover_transport(self, exc: Exception) -> None:
        await self._on_transport_recovery_attempt(exc)
        backoff = max(1.0, min(self._heartbeat_interval, 5.0))
        while True:
            try:
                if self._transport.connected:
                    await self._transport.disconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Edge transport disconnect during recovery failed", exc_info=True)

            try:
                await self._transport.connect()
                await self._publish_node_card(self._resolved_node_card())
                await self._on_transport_recovered()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Edge transport recovery attempt failed for node=%s", self.node_id, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _on_transport_recovery_attempt(self, _exc: Exception) -> None:
        """Hook for subclasses to mirror recovery state."""

    async def _on_transport_recovered(self) -> None:
        """Hook for subclasses after transport recovery succeeds."""

    async def _on_rpc_request(self, topic: str, message: MeshMessage) -> None:
        if message.message_type != MessageType.RPC_REQUEST:
            return
        if message.target_node not in {"", self.node_id}:
            return

        request_id = str(message.payload.get("request_id") or "")
        tool_name = str(message.payload.get("tool_name") or "")
        arguments = dict(message.payload.get("arguments") or {})
        try:
            await self._ensure_tool_approved(
                tool_name,
                arguments,
                ApprovalRequestContext(source="rpc", source_node=message.source_node),
            )
            result = await self._execute_tool(tool_name, arguments)
        except Exception as exc:
            result = ToolResult(
                call_id=request_id or "approval-error",
                tool_name=tool_name,
                success=False,
                output="",
                error=str(exc),
            )

        response_topic = f"nexus/rpc/{message.source_node}/{request_id}/response"
        response = self._transport.make_message(
            MessageType.RPC_RESPONSE,
            response_topic,
            {
                "request_id": request_id,
                "ok": result.success,
                "success": result.success,
                "tool_name": result.tool_name,
                "output": result.output,
                "error": result.error,
                "duration_ms": result.duration_ms,
            },
            target_node=message.source_node,
        )
        await self._transport.publish(response_topic, response)

    async def _on_task_assigned(self, topic: str, message: MeshMessage) -> None:
        if message.message_type != MessageType.TASK_ASSIGN:
            return

        assignment = TaskAssignment.from_dict(message.payload)
        if assignment.assigned_node and assignment.assigned_node != self.node_id:
            return

        execution_mode = str(assignment.metadata.get("execution_mode", "tool"))
        if execution_mode == "agent_loop":
            await self._execute_agent_loop(assignment)
        else:
            await self._execute_single_tool(assignment)

    async def _execute_single_tool(self, assignment: TaskAssignment) -> None:
        """Original single-tool execution path."""
        try:
            if self._tool_requires_approval(assignment.tool_name):
                await self._publish_task_status(assignment, TaskStepState.WAITING_APPROVAL)
            await self._ensure_tool_approved(
                assignment.tool_name,
                assignment.arguments,
                ApprovalRequestContext(
                    source="task",
                    source_node=assignment.assigned_node,
                    task_id=assignment.task_id,
                    step_id=assignment.step_id,
                    timeout_seconds=assignment.timeout_seconds,
                    metadata=dict(assignment.metadata),
                ),
            )
            await self._publish_task_status(assignment, TaskStepState.RUNNING)
            result = await self._execute_tool(assignment.tool_name, assignment.arguments)
        except Exception as exc:
            result = ToolResult(
                call_id=assignment.step_id or assignment.task_id,
                tool_name=assignment.tool_name,
                success=False,
                output="",
                error=str(exc),
            )
        await self._publish_task_status(
            assignment,
            TaskStepState.SUCCEEDED if result.success else TaskStepState.FAILED,
            error=result.error,
        )
        await self._publish_task_result(assignment, result)

    async def _execute_agent_loop(self, assignment: TaskAssignment) -> None:
        """Delegated agent-loop execution — Hub planned, edge LLM drives multi-step."""
        if self._edge_runtime is None:
            result = ToolResult(
                call_id=assignment.step_id or assignment.task_id,
                tool_name=assignment.tool_name,
                success=False,
                output="",
                error="No EdgeAgentRuntime configured — agent_loop execution unavailable",
            )
            await self._publish_task_status(assignment, TaskStepState.FAILED, error=result.error)
            await self._publish_task_result(assignment, result)
            return

        await self._publish_task_status(assignment, TaskStepState.RUNNING)
        self._active_executions += 1
        try:
            task_description = str(assignment.metadata.get("task_description") or assignment.tool_name)
            constraints = dict(assignment.metadata.get("constraints") or {})
            run_result = await self._edge_runtime.run_delegated(
                task_description,
                constraints=constraints if constraints else None,
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
            self._active_executions = max(0, self._active_executions - 1)

        await self._publish_task_status(
            assignment,
            TaskStepState.SUCCEEDED if result.success else TaskStepState.FAILED,
            error=result.error,
        )
        await self._publish_task_result(assignment, result)

    async def _publish_task_status(
        self,
        assignment: TaskAssignment,
        state: TaskStepState,
        *,
        error: str | None = None,
    ) -> None:
        topic = task_status_topic(assignment.task_id)
        message = self._transport.make_message(
            MessageType.TASK_STATUS,
            topic,
            {
                "task_id": assignment.task_id,
                "step_id": assignment.step_id,
                "node_id": self.node_id,
                "tool_name": assignment.tool_name,
                "status": state.value,
                "error": error,
            },
        )
        await self._transport.publish(topic, message)

    async def _publish_task_result(self, assignment: TaskAssignment, result: ToolResult) -> None:
        topic = task_result_topic(assignment.task_id)
        payload = TaskExecutionResult(
            task_id=assignment.task_id,
            step_id=assignment.step_id,
            node_id=self.node_id,
            tool_name=result.tool_name,
            success=result.success,
            output=result.output,
            error=result.error,
            duration_ms=result.duration_ms,
            metadata=dict(assignment.metadata),
        )
        message = self._transport.make_message(
            MessageType.TASK_RESULT,
            topic,
            payload.to_dict(),
        )
        await self._transport.publish(topic, message)

    async def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        self._active_executions += 1
        try:
            return await self._tool_executor.execute(tool_name, arguments)
        finally:
            self._active_executions = max(0, self._active_executions - 1)

    def _tool_definition(self, tool_name: str) -> ToolDefinition | None:
        return self._tool_executor.definition(tool_name)

    def _tool_requires_approval(self, tool_name: str) -> bool:
        tool = self._tool_definition(tool_name)
        return bool(tool and tool.requires_approval)

    async def _ensure_tool_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ApprovalRequestContext,
    ) -> None:
        tool = self._tool_definition(tool_name)
        if tool is None:
            raise ValueError(f"Unknown edge tool: {tool_name}")
        if not tool.requires_approval:
            return
        if self._approval_handler is None:
            raise PermissionError(f"Tool '{tool_name}' requires approval but no approval handler is configured")
        await self._approval_handler(tool, arguments, context)

    async def _current_load(self) -> float:
        if self._load_provider is None:
            return min(1.0, self._active_executions / 4)
        value = self._load_provider()
        if asyncio.iscoroutine(value):
            value = await value
        return max(0.0, min(1.0, float(value)))

    async def _battery_level(self) -> float | None:
        if self._battery_level_provider is None:
            return None
        value = self._battery_level_provider()
        if asyncio.iscoroutine(value):
            value = await value
        if value is None:
            return None
        return max(0.0, min(100.0, float(value)))

    def _resolved_node_card(self) -> NodeCard:
        if self._node_card is not None:
            return NodeCard.from_dict(self._node_card.to_dict())
        assert self._node_card_path is not None
        return NodeCard.from_yaml_file(self._node_card_path)

    def _validated_node_card(self) -> NodeCard:
        card = self._resolved_node_card()
        tool_specs = self._tool_executor.tool_specs()

        missing_tools = sorted(tool_name for tool_name in card.tool_names() if tool_name not in tool_specs)
        if missing_tools:
            raise ValueError(
                f"NodeCard declares tools not implemented by the edge runtime: {', '.join(missing_tools)}"
            )

        for capability in card.capabilities:
            self._attach_tool_specs(capability, tool_specs)
        return card

    @staticmethod
    def _attach_tool_specs(capability: CapabilitySpec, tool_specs: dict[str, dict[str, Any]]) -> None:
        specs = {
            tool_name: tool_specs[tool_name]
            for tool_name in capability.tools
            if tool_name in tool_specs
        }
        capability.properties = dict(capability.properties or {})
        capability.properties["tool_specs"] = specs
