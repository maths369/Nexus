"""Remote tool proxy backed by mesh RPC."""

from __future__ import annotations

import asyncio
import base64
import json as _json_module
import uuid
from typing import Any

from nexus.agent.types import ToolDefinition, ToolRiskLevel

from .node_card import NodeType
from .registry import MeshRegistry
from .task_protocol import TaskAssignment, task_assign_topic, task_result_topic
from .transport import MeshMessage, MeshTransport, MessageType


def _risk_level(value: str | None) -> ToolRiskLevel:
    try:
        return ToolRiskLevel(value or ToolRiskLevel.MEDIUM.value)
    except ValueError:
        return ToolRiskLevel.MEDIUM


class RemoteToolProxy:
    """Expose mesh tools from remote nodes as local ToolDefinitions."""

    _ALIAS_PREFIX = "mesh"

    def __init__(
        self,
        *,
        transport: MeshTransport,
        registry: MeshRegistry,
        local_node_id: str,
        local_tool_names: set[str] | None = None,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        self._transport = transport
        self._registry = registry
        self._local_node_id = local_node_id
        self._local_tool_names = set(local_tool_names or set())
        self._default_timeout = default_timeout_seconds

    def build_remote_tools(self) -> list[ToolDefinition]:
        tools: dict[str, ToolDefinition] = {}
        for node in self._registry.list_nodes(online_only=True):
            if node.node_id == self._local_node_id:
                continue
            for capability in node.capabilities:
                tool_specs = dict(capability.properties.get("tool_specs") or {})
                for tool_name in capability.tools:
                    alias = self.alias_for(node.node_id, tool_name)
                    if alias in tools:
                        continue
                    spec = dict(tool_specs.get(tool_name) or {})
                    tools[alias] = ToolDefinition(
                        name=alias,
                        description=self._description_for(node.display_name, capability.description, spec),
                        parameters=dict(spec.get("parameters") or self._fallback_schema()),
                        handler=self._make_handler(alias),
                        risk_level=_risk_level(spec.get("risk_level")),
                        requires_approval=bool(spec.get("requires_approval", False)),
                        tags=self._tags_for(spec, node_id=node.node_id, underlying_tool=tool_name),
                    )
        return list(tools.values())

    _DISPATCH_PREFIX = "mesh_dispatch"

    def build_dispatch_tools(self) -> list[ToolDefinition]:
        """Build dispatch tools for agent-loop delegation to remote edge nodes."""
        tools: list[ToolDefinition] = []
        seen: set[str] = set()
        for node in self._registry.list_nodes(online_only=True):
            if node.node_id == self._local_node_id:
                continue
            if node.node_type != NodeType.EDGE:
                continue
            if node.node_id in seen:
                continue
            seen.add(node.node_id)
            alias = self.dispatch_alias_for(node.node_id)
            # Collect capability descriptions for richer tool description
            cap_names = [c.capability_id for c in node.capabilities]
            cap_summary = "、".join(cap_names) if cap_names else "通用任务"
            tools.append(ToolDefinition(
                name=alias,
                description=(
                    f"委托任务给 {node.display_name} ({node.node_id}) 执行。"
                    f"该节点能力：{cap_summary}。"
                    f"调用此工具后，{node.display_name} 会自主规划并执行多步操作"
                    f"（包括打开应用、浏览器操作、文件操作、截屏等）。"
                    f"你只需要描述任务目标，节点会自动完成。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_description": {
                            "type": "string",
                            "description": "要在该节点上执行的任务描述，用自然语言即可。例如：'打开Chrome浏览器' 或 '截取当前屏幕'。",
                        },
                        "constraints": {
                            "type": "string",
                            "description": "可选的约束条件（JSON 字符串）。",
                        },
                    },
                    "required": ["task_description"],
                },
                handler=self._make_dispatch_handler(node.node_id, alias),
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["mesh", "dispatch", f"node:{node.node_id}"],
            ))
        return tools

    @classmethod
    def dispatch_alias_for(cls, node_id: str) -> str:
        encoded = base64.urlsafe_b64encode(node_id.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{cls._DISPATCH_PREFIX}__{encoded}"

    def _make_dispatch_handler(self, target_node: str, tool_name: str):
        async def handler(*, task_description: str, constraints: str = "") -> str:
            return await self.dispatch_to_edge(
                target_node=target_node,
                task_description=task_description,
                constraints=constraints,
            )
        return handler

    async def dispatch_to_edge(
        self,
        *,
        target_node: str,
        task_description: str,
        constraints: str = "",
        timeout: float = 300.0,
    ) -> str:
        """Dispatch a multi-step task to an edge node via TaskAssignment."""
        task_id = f"dispatch-{uuid.uuid4().hex[:12]}"
        constraint_dict: dict[str, Any] = {}
        if constraints:
            try:
                constraint_dict = _json_module.loads(constraints)
            except _json_module.JSONDecodeError:
                constraint_dict = {"raw": constraints}

        assignment = TaskAssignment(
            task_id=task_id,
            step_id="dispatch-step-1",
            assigned_node=target_node,
            tool_name="agent_loop",
            timeout_seconds=timeout,
            metadata={
                "execution_mode": "agent_loop",
                "task_description": task_description,
                "constraints": constraint_dict,
            },
        )

        # Subscribe to result topic before publishing assignment
        result_topic = task_result_topic(task_id)
        result_future: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()

        async def _on_result(topic: str, message: MeshMessage) -> None:
            if message.message_type == MessageType.TASK_RESULT:
                payload = dict(message.payload)
                if not result_future.done():
                    result_future.set_result(payload)

        await self._transport.subscribe(result_topic, _on_result)
        try:
            # Publish the assignment
            assign_topic = task_assign_topic(task_id)
            msg = self._transport.make_message(
                MessageType.TASK_ASSIGN,
                assign_topic,
                assignment.to_dict(),
                target_node=target_node,
            )
            await self._transport.publish(assign_topic, msg)

            # Wait for result
            payload = await asyncio.wait_for(result_future, timeout=timeout)
        finally:
            await self._transport.unsubscribe(result_topic)

        if not bool(payload.get("success")):
            error = str(payload.get("error") or f"Edge agent-loop execution failed on {target_node}")
            raise RuntimeError(error)
        return str(payload.get("output") or "")

    async def execute(self, tool_name: str, arguments: dict[str, Any], *, timeout: float | None = None) -> str:
        target_node, actual_tool_name = self._resolve_route(tool_name)
        response = await self._transport.request(
            target_node,
            {"tool_name": actual_tool_name, "arguments": arguments},
            timeout=timeout or self._default_timeout,
            source_node=self._local_node_id,
        )
        payload = response.payload
        if not bool(payload.get("success", payload.get("ok"))):
            error = str(payload.get("error") or f"Remote tool {actual_tool_name}@{target_node} failed")
            raise RuntimeError(error)
        return str(payload.get("output") or "")

    def _make_handler(self, tool_name: str):
        async def handler(**kwargs):
            return await self.execute(tool_name, kwargs)

        return handler

    @classmethod
    def alias_for(cls, node_id: str, tool_name: str) -> str:
        encoded = base64.urlsafe_b64encode(node_id.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{cls._ALIAS_PREFIX}__{encoded}__{tool_name}"

    @classmethod
    def parse_alias(cls, tool_name: str) -> tuple[str, str] | None:
        parts = tool_name.split("__", 2)
        if len(parts) != 3 or parts[0] != cls._ALIAS_PREFIX:
            return None
        encoded_node_id = parts[1]
        padding = "=" * (-len(encoded_node_id) % 4)
        try:
            node_id = base64.urlsafe_b64decode((encoded_node_id + padding).encode("ascii")).decode("utf-8")
        except Exception:
            return None
        actual_tool_name = parts[2]
        if not node_id or not actual_tool_name:
            return None
        return node_id, actual_tool_name

    def _resolve_route(self, tool_name: str) -> tuple[str, str]:
        alias = self.parse_alias(tool_name)
        if alias is not None:
            return alias

        candidates = [
            node_id
            for node_id, _capability_id in self._registry.query_tool(tool_name, online_only=True)
            if node_id != self._local_node_id
        ]
        if not candidates:
            raise RuntimeError(f"No online remote node exposes tool: {tool_name}")
        return candidates[0], tool_name

    @staticmethod
    def _description_for(node_name: str, capability_description: str, spec: dict[str, Any]) -> str:
        description = str(spec.get("description") or capability_description or "").strip()
        if not description:
            description = "Remote mesh tool"
        return f"[Remote: {node_name}] {description}"

    @staticmethod
    def _tags_for(spec: dict[str, Any], *, node_id: str, underlying_tool: str) -> list[str]:
        tags = ["mesh", "remote"]
        tags.append(f"node:{node_id}")
        tags.append(f"tool:{underlying_tool}")
        tags.extend(str(tag) for tag in spec.get("tags") or [])
        return list(dict.fromkeys(tags))

    @staticmethod
    def _fallback_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }
