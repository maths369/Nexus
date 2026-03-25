"""Mesh-aware task planning and route selection."""

from __future__ import annotations

import enum
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from nexus.agent.types import ToolDefinition
from nexus.provider.gateway import ProviderGateway

from .node_card import NodeType
from .registry import MeshRegistry
from .remote_tools import RemoteToolProxy

logger = logging.getLogger(__name__)


class StepState(str, enum.Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_FOR_NODE = "waiting_for_node"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanState(str, enum.Enum):
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_NODE = "waiting_for_node"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class TaskStep:
    step_id: str
    description: str
    required_capabilities: list[str] = field(default_factory=list)
    preferred_tools: list[str] = field(default_factory=list)
    assigned_node: str | None = None
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: float = 900.0
    state: StepState = StepState.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "required_capabilities": list(self.required_capabilities),
            "preferred_tools": list(self.preferred_tools),
            "assigned_node": self.assigned_node,
            "depends_on": list(self.depends_on),
            "timeout_seconds": self.timeout_seconds,
            "state": self.state.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskStep":
        return cls(
            step_id=str(data.get("step_id") or ""),
            description=str(data.get("description") or ""),
            required_capabilities=[str(item) for item in data.get("required_capabilities") or []],
            preferred_tools=[str(item) for item in data.get("preferred_tools") or []],
            assigned_node=str(data.get("assigned_node")) if data.get("assigned_node") else None,
            depends_on=[str(item) for item in data.get("depends_on") or []],
            timeout_seconds=float(data.get("timeout_seconds") or 900.0),
            state=StepState(str(data.get("state") or StepState.PENDING.value)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class TaskPlan:
    task_id: str
    session_id: str
    user_task: str
    steps: list[TaskStep] = field(default_factory=list)
    state: PlanState = PlanState.READY
    metadata: dict[str, Any] = field(default_factory=dict)

    def refresh_state(self) -> PlanState:
        if any(step.state == StepState.FAILED for step in self.steps):
            self.state = PlanState.FAILED
        elif all(step.state == StepState.WAITING_FOR_NODE for step in self.steps):
            # Only block if ALL steps are waiting — partial waits are OK
            self.state = PlanState.WAITING_FOR_NODE
        elif self.steps and all(step.state == StepState.COMPLETED for step in self.steps):
            self.state = PlanState.COMPLETED
        elif any(step.state == StepState.RUNNING for step in self.steps):
            self.state = PlanState.RUNNING
        else:
            self.state = PlanState.READY
        return self.state

    def waiting_node_ids(self) -> list[str]:
        waiting: list[str] = []
        seen: set[str] = set()
        for step in self.steps:
            if step.state != StepState.WAITING_FOR_NODE:
                continue
            for node_id in step.metadata.get("waiting_node_ids", []) or []:
                node = str(node_id).strip()
                if node and node not in seen:
                    seen.add(node)
                    waiting.append(node)
        return waiting

    def remote_nodes(self, local_node_id: str) -> list[str]:
        remote: list[str] = []
        seen: set[str] = set()
        for step in self.steps:
            node_id = step.assigned_node or ""
            if not node_id or node_id == local_node_id or node_id in seen:
                continue
            seen.add(node_id)
            remote.append(node_id)
        return remote

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "user_task": self.user_task,
            "steps": [step.to_dict() for step in self.steps],
            "state": self.state.value,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskPlan":
        plan = cls(
            task_id=str(data.get("task_id") or ""),
            session_id=str(data.get("session_id") or ""),
            user_task=str(data.get("user_task") or ""),
            steps=[TaskStep.from_dict(item) for item in data.get("steps") or []],
            state=PlanState(str(data.get("state") or PlanState.READY.value)),
            metadata=dict(data.get("metadata") or {}),
        )
        plan.refresh_state()
        return plan


@dataclass(slots=True)
class TaskRoutingContext:
    plan: TaskPlan
    effective_task: str
    extra_tools: list[ToolDefinition] = field(default_factory=list)
    disabled_local_tools: list[str] = field(default_factory=list)
    status_message: str = ""
    blocked_reason: str | None = None


@dataclass(slots=True)
class NodeScore:
    node_id: str
    capability_ids: list[str]
    score: float
    reasons: list[str] = field(default_factory=list)


class RoutingPolicy:
    """Score candidate nodes for a task step."""

    def score_node(self, registry: MeshRegistry, node_id: str, step: TaskStep) -> NodeScore:
        card = registry.get_node(node_id)
        status = registry.get_node_status(node_id)
        score = 0.0
        reasons: list[str] = []

        if card is None:
            return NodeScore(node_id=node_id, capability_ids=list(step.required_capabilities), score=10_000.0)

        if not status or not status.online:
            score += 1_000
            reasons.append("offline")

        load = float(status.current_load if status else 0.0)
        score += load * 100.0
        if load > 0:
            reasons.append(f"load={load:.2f}")

        if bool(step.metadata.get("long_running")) and card.resources.battery_powered:
            score += 35.0
            reasons.append("battery_penalty")

        if bool(step.metadata.get("requires_user_interaction")):
            if card.node_type == NodeType.EDGE:
                score -= 12.0
                reasons.append("interactive_edge_bonus")
            elif card.node_type == NodeType.HUB:
                score += 18.0
                reasons.append("interactive_hub_penalty")

        if bool(step.metadata.get("privacy_local_only")):
            if card.has_local_llm() or "local_llm_inference" in card.capability_ids():
                score -= 15.0
                reasons.append("local_privacy_bonus")
            else:
                score += 40.0
                reasons.append("no_local_privacy_penalty")

        if bool(step.metadata.get("preferred_hub")) and card.node_type == NodeType.HUB:
            score -= 10.0
            reasons.append("hub_bonus")

        if bool(step.metadata.get("preferred_edge")) and card.node_type == NodeType.EDGE:
            score -= 10.0
            reasons.append("edge_bonus")

        max_duration = int(card.availability.max_task_duration_seconds or 0)
        timeout_seconds = float(step.timeout_seconds or 0)
        if max_duration > 0 and timeout_seconds > max_duration:
            score += 30.0
            reasons.append("duration_penalty")

        return NodeScore(
            node_id=node_id,
            capability_ids=list(step.required_capabilities),
            score=score,
            reasons=reasons,
        )


class TaskRouter:
    """Plan mesh-aware execution and expose remote tools for the local run."""

    # 旧的 _CAPABILITY_RULES / _AUTOMATION_APPS / _HEURISTIC 相关常量已移除。
    # LLM-driven routing: 所有路由决策由 LLM 在 agent loop 中自行判断。

    def __init__(
        self,
        *,
        registry: MeshRegistry,
        local_node_id: str,
        provider: ProviderGateway | None = None,
        transport: Any | None = None,
        local_tool_names: set[str] | None = None,
        capability_manager: Any | None = None,
        routing_policy: RoutingPolicy | None = None,
        planner_mode: str = "auto",
        task_manager: Any | None = None,
    ) -> None:
        self._registry = registry
        self._local_node_id = local_node_id
        self._provider = provider
        self._capability_manager = capability_manager
        self._routing_policy = routing_policy or RoutingPolicy()
        self._planner_mode = planner_mode
        self._plans: dict[str, TaskPlan] = {}
        self._local_tool_names = set(local_tool_names or set())
        self._local_runtime_capabilities: dict[str, dict[str, Any]] = {}
        self._remote_proxy = (
            RemoteToolProxy(
                transport=transport,
                registry=registry,
                local_node_id=local_node_id,
                local_tool_names=self._local_tool_names,
                task_manager=task_manager,
            )
            if transport is not None
            else None
        )

        self._registry.on_node_offline(self._on_node_offline)
        self._registry.on_node_online(self._on_node_online)

    def get_session_plan(self, session_id: str) -> TaskPlan | None:
        plan = self._plans.get(session_id)
        if plan is None:
            return None
        return TaskPlan.from_dict(plan.to_dict())

    async def prepare_run(
        self,
        *,
        session_id: str,
        task: str,
        context_messages: list[dict[str, Any]],
        route_hint: str = "auto",
    ) -> TaskRoutingContext:
        # ── LLM-driven routing ──
        # 不再做关键词预规划。所有任务默认在 Hub 本地执行，
        # 同时注入所有在线 edge 节点的 mesh_dispatch 工具。
        # LLM 根据 system prompt 中的路由规则自行决定是否 dispatch。
        step = TaskStep(
            step_id="step-1",
            description="在 Hub 本地执行任务（LLM 按需 dispatch 到边缘节点）",
            required_capabilities=[],
            preferred_tools=[],
            timeout_seconds=900.0,
        )
        step.assigned_node = self._local_node_id
        step.state = StepState.ASSIGNED
        plan = TaskPlan(
            task_id=f"mesh-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_task=task,
            steps=[step],
            metadata={"local_node_id": self._local_node_id, "route_hint": route_hint},
        )
        plan.refresh_state()
        self._plans[session_id] = plan

        # 始终注入在线 edge 节点的 dispatch 工具，让 LLM 可用
        extra_tools = self._dispatch_tools_for_online_edges()
        return TaskRoutingContext(
            plan=plan,
            effective_task=self._augment_task(task, plan),
            extra_tools=extra_tools,
            disabled_local_tools=[],  # 不禁用任何本地工具
            status_message="",
            blocked_reason=None,
        )

    async def plan_task(
        self,
        *,
        session_id: str,
        task: str,
        context: list[dict[str, Any]],
        route_hint: str = "auto",
    ) -> TaskPlan:
        """创建执行计划。LLM-driven：默认单步本地执行，LLM 按需 dispatch。"""
        step = TaskStep(
            step_id="step-1",
            description="在 Hub 本地执行任务（LLM 按需 dispatch 到边缘节点）",
            required_capabilities=[],
            preferred_tools=[],
            timeout_seconds=900.0,
        )
        step.assigned_node = self._local_node_id
        step.state = StepState.ASSIGNED

        plan = TaskPlan(
            task_id=f"mesh-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_task=task,
            steps=[step],
            metadata={"local_node_id": self._local_node_id, "route_hint": route_hint},
        )
        plan.refresh_state()
        self._plans[session_id] = plan
        return plan

    async def assign_step(self, step: TaskStep, *, exclude_nodes: list[str] | None = None) -> str:
        excluded = set(exclude_nodes or [])
        if not step.required_capabilities:
            step.assigned_node = self._local_node_id
            step.state = StepState.ASSIGNED
            step.metadata["routing_reasons"] = ["no_capability_requirement"]
            return self._local_node_id

        coverage: dict[str, set[str]] = {}
        offline_coverage: dict[str, set[str]] = {}
        for capability_id in step.required_capabilities:
            entries = self._registry.query_capability(capability_id, online_only=False, exclude_nodes=list(excluded))
            if not entries:
                local_status = await self._ensure_local_capability_ready(capability_id)
                if local_status is not None:
                    coverage.setdefault(self._local_node_id, set()).add(capability_id)
                    auto_local = step.metadata.setdefault("auto_local_capabilities", [])
                    if capability_id not in auto_local:
                        auto_local.append(capability_id)
                    continue
                # Fallback: edge nodes (macOS) can handle ANY local
                # operation via AppleScript/automation — no need to
                # register every capability individually.
                edge_fallback = self._find_online_edge_node(excluded)
                if edge_fallback:
                    coverage.setdefault(edge_fallback, set()).add(capability_id)
                    step.metadata.setdefault("edge_fallback_capabilities", []).append(capability_id)
                    logger.info(
                        "Capability %s not registered — falling back to edge node %s",
                        capability_id, edge_fallback,
                    )
                    continue
                step.assigned_node = None
                step.state = StepState.WAITING_FOR_NODE
                step.metadata["missing_capability_ids"] = [capability_id]
                return ""
            for entry in entries:
                target = coverage if self._is_online(entry.node_id) else offline_coverage
                target.setdefault(entry.node_id, set()).add(capability_id)

        online_candidates = [
            node_id
            for node_id, caps in coverage.items()
            if len(caps) == len(step.required_capabilities) and node_id not in excluded
        ]
        offline_candidates = [
            node_id
            for node_id, caps in offline_coverage.items()
            if len(caps) == len(step.required_capabilities) and node_id not in excluded
        ]

        if not online_candidates:
            step.assigned_node = None
            step.state = StepState.WAITING_FOR_NODE
            step.metadata["waiting_node_ids"] = offline_candidates
            return ""

        scored = sorted(
            (self._routing_policy.score_node(self._registry, node_id, step) for node_id in online_candidates),
            key=lambda item: item.score,
        )
        best = scored[0]
        step.assigned_node = best.node_id
        step.state = StepState.ASSIGNED
        step.metadata["routing_reasons"] = list(best.reasons)
        step.metadata["waiting_node_ids"] = []
        if best.node_id == self._local_node_id and step.metadata.get("auto_local_capabilities"):
            step.metadata["routing_reasons"].append("local_self_evolution")

        # Mark steps for agent-loop execution when assigned to remote edge nodes
        # with multi-step capabilities (browser, filesystem, screen, etc.)
        if best.node_id != self._local_node_id:
            card = self._registry.get_node(best.node_id)
            if card is not None and card.node_type == NodeType.EDGE:
                if self._should_use_agent_loop(step):
                    step.metadata["execution_mode"] = "agent_loop"

        return best.node_id

    async def handle_node_offline(self, node_id: str) -> list[str]:
        updated_sessions: list[str] = []
        for session_id, plan in self._plans.items():
            changed = False
            for step in plan.steps:
                if step.assigned_node != node_id or step.state in {StepState.COMPLETED, StepState.FAILED}:
                    continue
                previous = step.assigned_node
                rerouted = await self.assign_step(step, exclude_nodes=[node_id])
                if rerouted:
                    step.metadata["rerouted_from"] = previous
                changed = True
            if changed:
                plan.refresh_state()
                updated_sessions.append(session_id)
        return updated_sessions

    async def handle_node_online(self, node_id: str) -> list[str]:
        updated_sessions: list[str] = []
        for session_id, plan in self._plans.items():
            changed = False
            for step in plan.steps:
                waiting_nodes = set(str(item) for item in step.metadata.get("waiting_node_ids", []) or [])
                if step.state != StepState.WAITING_FOR_NODE:
                    continue
                if waiting_nodes and node_id not in waiting_nodes:
                    continue
                assigned = await self.assign_step(step)
                changed = changed or bool(assigned)
            if changed:
                plan.refresh_state()
                updated_sessions.append(session_id)
        return updated_sessions

    def mark_session_plan_running(self, session_id: str) -> None:
        plan = self._plans.get(session_id)
        if plan is None:
            return
        for step in plan.steps:
            if step.state == StepState.ASSIGNED:
                step.state = StepState.RUNNING
        plan.refresh_state()

    def mark_session_plan_finished(self, session_id: str, *, success: bool) -> None:
        plan = self._plans.get(session_id)
        if plan is None:
            return
        terminal = StepState.COMPLETED if success else StepState.FAILED
        for step in plan.steps:
            if step.state not in {StepState.COMPLETED, StepState.FAILED, StepState.WAITING_FOR_NODE}:
                step.state = terminal
        plan.refresh_state()

    def render_status(self, plan: TaskPlan) -> str:
        lines = [f"Mesh 计划：{plan.state.value}，共 {len(plan.steps)} 步。"]
        for idx, step in enumerate(plan.steps, start=1):
            node_label = step.assigned_node or "待节点上线"
            lines.append(f"{idx}. {step.description} -> {node_label} [{step.state.value}]")
        return "\n".join(lines)

    def _augment_task(self, task: str, plan: TaskPlan) -> str:
        if not plan.steps:
            return task
        lines = [
            task.strip(),
            "",
            "## Mesh 执行上下文",
            self.render_status(plan),
        ]

        # 描述可用的边缘节点和 dispatch 工具，LLM 自行决定是否委托
        online_edge_nodes = self._list_online_edge_nodes()
        if online_edge_nodes and self._remote_proxy is not None:
            lines.append("")
            lines.append("## 可用的边缘节点")
            lines.append(
                "以下节点在线，你可以随时将任务委托给它们执行。\n"
                "**判断原则**：如果任务需要操作 Mac 上的应用、浏览器、摄像头、文件系统、"
                "剪贴板、或任何本地资源，**必须**通过对应的 dispatch 工具委托给边缘节点。\n"
                "Hub（云端）无法操作用户的 Mac——只有边缘节点可以。\n"
                "你只需描述任务目标，节点会自主完成所有操作步骤。"
            )
            for node_id, card in online_edge_nodes:
                dispatch_alias = self._remote_proxy.dispatch_alias_for(node_id)
                display = card.display_name if card else node_id
                cap_names = [c.capability_id for c in card.capabilities] if card else []
                cap_summary = "、".join(cap_names) if cap_names else "通用 Mac 自动化（AppleScript、浏览器、文件系统等）"
                lines.append(
                    f"- `{dispatch_alias}` → **{display}**：{cap_summary}"
                )

        return "\n".join(lines).strip()

    def _dispatch_tools_for_online_edges(self) -> list[ToolDefinition]:
        """注入所有在线 edge 节点的 dispatch 工具，让 LLM 自行决定是否使用。"""
        if self._remote_proxy is None:
            return []
        return list(self._remote_proxy.build_dispatch_tools())


    async def _ensure_local_capability_ready(self, capability_id: str) -> dict[str, Any] | None:
        if self._capability_manager is None:
            return None
        try:
            status = dict(self._capability_manager.get_status(capability_id))
        except Exception:
            logger.warning("Failed to get local capability status for %s", capability_id, exc_info=True)
            return None

        if not bool(status.get("known")):
            return None

        if not bool(status.get("enabled")):
            try:
                result = await self._capability_manager.enable(capability_id, actor="mesh_router")
            except Exception:
                logger.warning("Failed to auto-enable local capability %s", capability_id, exc_info=True)
                return None
            if not bool(getattr(result, "success", False)):
                return None
            status = dict(self._capability_manager.get_status(capability_id))

        self._local_runtime_capabilities[capability_id] = status
        return status

    def _local_tools_for_capability(self, capability_id: str) -> list[str]:
        cached = self._local_runtime_capabilities.get(capability_id)
        if cached is None and self._capability_manager is not None:
            try:
                status = dict(self._capability_manager.get_status(capability_id))
            except Exception:
                logger.warning("Failed to refresh local capability tools for %s", capability_id, exc_info=True)
                return []
            if bool(status.get("known")):
                cached = status
                self._local_runtime_capabilities[capability_id] = status
        if cached is None:
            return []
        return [str(tool) for tool in cached.get("tools", []) or [] if str(tool).strip()]


    # Capabilities that benefit from multi-step agent-loop execution
    _AGENT_LOOP_CAPS = {"browser_automation", "screen_capture", "local_filesystem", "audio_recording", "video_capture", "apple_shortcuts", "apple_automation"}

    def _should_use_agent_loop(self, step: TaskStep) -> bool:
        """Determine if a step should use agent-loop execution on the edge node.

        Criteria:
        - Step has capabilities that inherently need multi-step interaction
        - OR step has 2+ required capabilities (complex task)
        """
        caps = set(step.required_capabilities)
        if caps & self._AGENT_LOOP_CAPS:
            return True
        if len(caps) >= 2:
            return True
        return False

    def get_agent_loop_steps(self, plan: TaskPlan) -> list[TaskStep]:
        """Return steps that should be dispatched as agent-loop TaskAssignments."""
        return [
            step for step in plan.steps
            if step.metadata.get("execution_mode") == "agent_loop"
            and step.assigned_node
            and step.assigned_node != self._local_node_id
        ]

    def _find_online_edge_node(self, excluded: set[str] | None = None) -> str | None:
        """Find any online edge node — Mac nodes can handle all local ops."""
        excluded = excluded or set()
        for card in self._registry.list_nodes(online_only=True, node_type=NodeType.EDGE):
            if card.node_id not in excluded:
                return card.node_id
        return None

    def _list_online_edge_nodes(self) -> list[tuple[str, Any]]:
        """Return all online edge nodes as (node_id, NodeCard) pairs."""
        result = []
        for card in self._registry.list_nodes(online_only=True, node_type=NodeType.EDGE):
            if card.node_id != self._local_node_id:
                result.append((card.node_id, card))
        return result

    def _previous_edge_node(self, session_id: str) -> str | None:
        """Return the edge node used in the previous plan for this session, if any."""
        prev_plan = self._plans.get(session_id)
        if prev_plan is None:
            return None
        for step in prev_plan.steps:
            if step.assigned_node and step.assigned_node != self._local_node_id:
                card = self._registry.get_node(step.assigned_node)
                if card is not None and card.node_type == NodeType.EDGE:
                    return step.assigned_node
        return None

    def _is_online(self, node_id: str) -> bool:
        status = self._registry.get_node_status(node_id)
        return bool(status and status.online)

    async def _on_node_offline(self, node_id: str, _card: Any) -> None:
        await self.handle_node_offline(node_id)

    async def _on_node_online(self, node_id: str, _card: Any) -> None:
        await self.handle_node_online(node_id)

    @staticmethod
    def _extract_json(text: str) -> Any:
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
