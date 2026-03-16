"""Mesh-aware task planning and route selection."""

from __future__ import annotations

import enum
import json
import logging
import re
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
        elif any(step.state == StepState.WAITING_FOR_NODE for step in self.steps):
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

    _CAPABILITY_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        ("browser_automation", ("浏览器", "browser", "chrome", "谷歌浏览器", "google chrome", "safari", "firefox", "edge", "brave", "网页", "网站", "登录", "抓取", "爬取", "网页内容", "表单"), ("browser_navigate", "browser_extract_text", "browser_fill_form", "browser_screenshot")),
        ("local_filesystem", ("文件", "文件夹", "目录", "本地文件", "工作区", "读取文件"), ("list_local_files", "code_read_file")),
        ("screen_capture", ("截图", "屏幕", "录屏", "屏幕录制"), ("capture_screen", "record_screen")),
        ("clipboard", ("剪贴板", "复制", "粘贴"), ("read_clipboard", "write_clipboard")),
        ("audio_recording", ("录音", "麦克风"), ("record_audio", "stop_recording")),
        ("video_capture", ("录像", "摄像头", "拍视频", "录视频"), ("capture_video",)),
        ("notifications", ("通知", "提醒", "推送"), ("send_notification",)),
        ("apple_shortcuts", ("快捷指令", "shortcuts", "自动化"), ("run_shortcut", "list_shortcuts")),
        ("apple_automation", ("applescript", "jxa", "打开应用", "启动应用", "激活应用", "前台窗口", "切换窗口", "系统脚本", "mac 自动化"), ("run_applescript",)),
        ("audio_transcription", ("转录", "语音识别", "音频转文字"), ("audio_transcribe_path",)),
        ("long_running_analysis", ("分析", "整理", "总结", "持续", "长时间", "不间断", "后台", "批处理"), ("background_run",)),
        ("knowledge_store", ("知识库", "入库", "索引", "归档", "检索"), ("knowledge_ingest", "search_vault", "write_vault", "read_vault")),
        ("document_management", ("文档", "笔记", "页面", "纪要"), ("create_note", "document_append_block", "document_replace_section")),
        ("local_llm_inference", ("敏感", "隐私", "本地模型", "不出本地"), ("local_llm_generate",)),
    )

    _ACQUISITION_CAPS = {"browser_automation", "local_filesystem", "screen_capture", "clipboard", "audio_recording", "video_capture", "apple_shortcuts", "apple_automation"}
    _PROCESSING_CAPS = {"audio_transcription", "long_running_analysis", "knowledge_store", "document_management", "local_llm_inference"}
    _DELIVERY_CAPS = {"notifications"}
    _OPEN_ACTIONS = ("打开", "启动", "激活", "切换到", "切到", "唤起")
    _BROWSER_APPS = ("chrome", "googlechrome", "google chrome", "谷歌浏览器", "safari", "firefox", "edge", "brave")
    _AUTOMATION_APPS = _BROWSER_APPS + ("finder", "访达", "terminal", "终端", "system settings", "系统设置")
    _FEISHU_DELIVERY_HINTS = (
        "发到飞书",
        "发送到飞书",
        "推送到飞书",
        "通知到飞书",
        "通过飞书通知",
        "通过飞书发给我",
        "回到飞书",
        "回复到飞书",
        "在飞书上通知",
    )

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
    ) -> TaskRoutingContext:
        plan = await self.plan_task(session_id=session_id, task=task, context=context_messages)
        self._plans[session_id] = plan

        blocked_reason = self._blocked_reason(plan)
        extra_tools = [] if blocked_reason else self._extra_tools_for_plan(plan)
        disabled_local_tools = [] if blocked_reason else self._disabled_local_tools_for_plan(plan)
        return TaskRoutingContext(
            plan=plan,
            effective_task=self._augment_task(task, plan),
            extra_tools=extra_tools,
            disabled_local_tools=disabled_local_tools,
            status_message=self.render_status(plan),
            blocked_reason=blocked_reason,
        )

    async def plan_task(
        self,
        *,
        session_id: str,
        task: str,
        context: list[dict[str, Any]],
    ) -> TaskPlan:
        steps = await self._build_steps(task, context)
        plan = TaskPlan(
            task_id=f"mesh-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_task=task,
            steps=steps,
            metadata={"local_node_id": self._local_node_id},
        )
        for step in plan.steps:
            await self.assign_step(step)
        plan.refresh_state()
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
        mesh_context = self._registry.build_mesh_context(online_only=False)
        lines = [
            task.strip(),
            "",
            "## Mesh 执行上下文",
            self.render_status(plan),
            "",
            "## 当前网络节点",
            mesh_context,
            "",
            "执行约束：",
        ]
        lines.append("- 优先按上述节点边界调用工具。")
        lines.append("- 远端节点工具使用 `mesh__...` 形式的节点作用域工具名。")
        lines.append("- 不要假设离线节点可用；若计划里显示 `waiting_for_node`，先说明阻塞原因。")
        lines.append("")
        lines.append("## 节点作用域工具")
        for idx, step in enumerate(plan.steps, start=1):
            node_label = step.assigned_node or "待节点上线"
            scoped_tools = self._scoped_tool_names_for_step(step)
            if scoped_tools:
                lines.append(
                    f"- 步骤 {idx} 由 `{node_label}` 执行，仅使用这些工具："
                    + ", ".join(f"`{name}`" for name in scoped_tools)
                )
            else:
                lines.append(f"- 步骤 {idx} 由 `{node_label}` 执行。")
        # Instructions for agent-loop delegation
        agent_loop_steps = self.get_agent_loop_steps(plan)
        if agent_loop_steps and self._remote_proxy is not None:
            lines.append("")
            lines.append("## 重要：你必须使用以下工具来完成任务")
            lines.append("这些步骤需要在远端设备上执行，你**必须调用对应的工具**，不要自己尝试回答或拒绝。")
            for step in agent_loop_steps:
                dispatch_alias = self._remote_proxy.dispatch_alias_for(step.assigned_node or "")
                node_name = step.assigned_node or "unknown"
                card = self._registry.get_node(node_name)
                display = card.display_name if card else node_name
                lines.append(
                    f"- 调用 `{dispatch_alias}` 工具，"
                    f"在 task_description 参数中填写你需要 {display} 执行的任务。"
                    f"例如：用户说'打开Chrome'，你就调用此工具并传入 task_description='打开Chrome浏览器'。"
                )

        disabled_local_tools = self._disabled_local_tools_for_plan(plan)
        if disabled_local_tools:
            lines.append("")
            lines.append(
                "- 为避免误用，本轮已屏蔽以下 Hub 本地同名工具："
                + ", ".join(f"`{name}`" for name in disabled_local_tools)
            )
        return "\n".join(lines).strip()

    def _extra_tools_for_plan(self, plan: TaskPlan) -> list[ToolDefinition]:
        if self._remote_proxy is None:
            return []
        extra: list[ToolDefinition] = []

        # Add dispatch tools for agent-loop steps
        agent_loop_steps = self.get_agent_loop_steps(plan)
        if agent_loop_steps:
            dispatch_defs = {tool.name: tool for tool in self._remote_proxy.build_dispatch_tools()}
            dispatched_nodes: set[str] = set()
            for step in agent_loop_steps:
                node_id = step.assigned_node or ""
                if node_id and node_id not in dispatched_nodes:
                    alias = self._remote_proxy.dispatch_alias_for(node_id)
                    if alias in dispatch_defs:
                        extra.append(dispatch_defs[alias])
                        dispatched_nodes.add(node_id)

        # Add individual remote tools for non-agent-loop steps
        remote_defs = {tool.name: tool for tool in self._remote_proxy.build_remote_tools()}
        required = self._remote_tool_names_for_plan(plan)
        extra.extend(remote_defs[name] for name in sorted(required) if name in remote_defs)
        return extra

    def _remote_tool_names_for_plan(self, plan: TaskPlan) -> set[str]:
        names: set[str] = set()
        for step in plan.steps:
            if not step.assigned_node or step.assigned_node == self._local_node_id:
                continue
            if step.state == StepState.WAITING_FOR_NODE:
                continue
            # Agent-loop steps use dispatch tools, not individual remote tools
            if step.metadata.get("execution_mode") == "agent_loop":
                continue
            for tool_name in self._plain_tool_names_for_step(step):
                if self._remote_proxy is None:
                    names.add(tool_name)
                else:
                    names.add(self._remote_proxy.alias_for(step.assigned_node, tool_name))
        return names

    def _disabled_local_tools_for_plan(self, plan: TaskPlan) -> list[str]:
        remote_plain: set[str] = set()
        local_plain: set[str] = set()
        has_remote_steps = False

        for step in plan.steps:
            if not step.assigned_node or step.state == StepState.WAITING_FOR_NODE:
                continue
            tool_names = set(self._plain_tool_names_for_step(step))
            if step.assigned_node == self._local_node_id:
                local_plain.update(tool_names)
            else:
                has_remote_steps = True
                remote_plain.update(tool_names)

        disabled = {
            tool_name
            for tool_name in remote_plain
            if tool_name in self._local_tool_names and tool_name not in local_plain
        }
        if has_remote_steps and "system_run" in self._local_tool_names:
            disabled.add("system_run")
        return sorted(disabled)

    def _scoped_tool_names_for_step(self, step: TaskStep) -> list[str]:
        tool_names = self._plain_tool_names_for_step(step)
        if not step.assigned_node or step.assigned_node == self._local_node_id or self._remote_proxy is None:
            return tool_names
        return [self._remote_proxy.alias_for(step.assigned_node, tool_name) for tool_name in tool_names]

    def _plain_tool_names_for_step(self, step: TaskStep) -> list[str]:
        if step.preferred_tools:
            return list(step.preferred_tools)
        if step.assigned_node == self._local_node_id:
            local_names: list[str] = []
            for capability_id in step.required_capabilities:
                local_names.extend(self._local_tools_for_capability(capability_id))
            if local_names:
                return list(dict.fromkeys(local_names))
        card = self._registry.get_node(step.assigned_node or "")
        if card is None:
            return []
        names: list[str] = []
        for capability_id in step.required_capabilities:
            names.extend(card.find_tools_for_capability(capability_id))
        return list(dict.fromkeys(names))

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

    def _blocked_reason(self, plan: TaskPlan) -> str | None:
        if plan.state != PlanState.WAITING_FOR_NODE:
            return None
        waiting_ids = plan.waiting_node_ids()
        if waiting_ids:
            labels = []
            for node_id in waiting_ids:
                card = self._registry.get_node(node_id)
                labels.append(card.display_name if card else node_id)
            return f"当前需要以下节点上线后才能继续：{', '.join(labels)}。"
        missing_caps: list[str] = []
        for step in plan.steps:
            missing_caps.extend(str(item) for item in step.metadata.get("missing_capability_ids", []) or [])
        if missing_caps:
            return f"当前网络中缺少所需能力：{', '.join(sorted(set(missing_caps)))}。"
        return "当前网络中没有满足该任务约束的在线节点。"

    async def _build_steps(self, task: str, context: list[dict[str, Any]]) -> list[TaskStep]:
        heuristic_steps = self._heuristic_plan(task)
        if self._planner_mode != "heuristic" and self._provider is not None:
            steps = await self._plan_with_provider(task, context)
            if self._provider_plan_is_usable(
                task,
                provider_steps=steps,
                heuristic_steps=heuristic_steps,
            ):
                return steps
        return heuristic_steps

    async def _plan_with_provider(self, task: str, context: list[dict[str, Any]]) -> list[TaskStep]:
        mesh_context = self._registry.build_mesh_context()
        prompt = (
            "请把下面的任务分解成 1-3 个结构化步骤，并只返回 JSON。\n"
            "JSON 结构: {\"steps\": [{\"description\": str, \"required_capabilities\": [str], "
            "\"preferred_tools\": [str], \"metadata\": {\"long_running\": bool, "
            "\"requires_user_interaction\": bool, \"privacy_local_only\": bool}}]}\n"
            "必须使用当前网络里真实存在的 capability_id。"
            f"\n\n当前网络:\n{mesh_context}\n\n任务:\n{task}"
        )
        try:
            raw = await self._provider.generate(
                prompt=prompt,
                context="你是多节点任务规划器。只返回 JSON，不要加解释。",
                temperature=0.1,
                max_tokens=1200,
            )
        except Exception:
            logger.warning("Mesh provider planning failed, fallback to heuristic", exc_info=True)
            return []

        data = self._extract_json(raw)
        if not isinstance(data, dict):
            return []
        items = data.get("steps")
        if not isinstance(items, list) or not items:
            return []

        steps: list[TaskStep] = []
        previous_step_id = ""
        for idx, item in enumerate(items[:3], start=1):
            capabilities = [str(cap) for cap in item.get("required_capabilities") or [] if str(cap).strip()]
            metadata = dict(item.get("metadata") or {})
            step = TaskStep(
                step_id=f"step-{idx}",
                description=str(item.get("description") or f"步骤 {idx}"),
                required_capabilities=capabilities,
                preferred_tools=[str(tool) for tool in item.get("preferred_tools") or [] if str(tool).strip()],
                depends_on=[previous_step_id] if previous_step_id else [],
                timeout_seconds=1800.0 if metadata.get("long_running") else 900.0,
                metadata=metadata,
            )
            previous_step_id = step.step_id
            steps.append(step)
        return steps

    def _provider_plan_is_usable(
        self,
        task: str,
        *,
        provider_steps: list[TaskStep],
        heuristic_steps: list[TaskStep],
    ) -> bool:
        if not provider_steps:
            return False

        descriptions = " ".join(step.description for step in provider_steps).strip()
        refusal_hints = ("不存在", "无法", "不能", "不可", "缺少", "没有")
        if descriptions and any(token in descriptions for token in refusal_hints):
            logger.warning("Mesh provider plan contained refusal-style description; fallback to heuristic")
            return False

        provider_caps = {
            capability_id
            for step in provider_steps
            for capability_id in step.required_capabilities
        }
        known_caps = {
            capability.capability_id
            for card in self._registry.list_nodes(online_only=False)
            for capability in card.capabilities
        }
        known_caps |= self._known_local_runtime_capability_ids()
        if provider_caps and not provider_caps.issubset(known_caps):
            logger.warning(
                "Mesh provider plan referenced unknown capabilities=%s; fallback to heuristic",
                sorted(provider_caps - known_caps),
            )
            return False

        heuristic_caps = {
            capability_id
            for step in heuristic_steps
            for capability_id in step.required_capabilities
        }
        if heuristic_caps and not provider_caps:
            logger.warning(
                "Mesh provider plan omitted all detected capabilities=%s; fallback to heuristic",
                sorted(heuristic_caps),
            )
            return False

        acquisition_caps = heuristic_caps & self._ACQUISITION_CAPS
        if acquisition_caps and not (provider_caps & acquisition_caps):
            logger.warning(
                "Mesh provider plan missed local-resource capabilities=%s; fallback to heuristic",
                sorted(acquisition_caps),
            )
            return False

        compact = re.sub(r"\s+", "", task.lower())
        local_resource_tokens = (
            "macbook",
            "这台mac",
            "本机",
            "本地",
            "chrome",
            "googlechrome",
            "谷歌浏览器",
            "safari",
            "finder",
            "访达",
        )
        if any(token in compact for token in local_resource_tokens):
            if not (provider_caps & self._ACQUISITION_CAPS):
                logger.warning("Mesh provider plan ignored explicit local-device intent; fallback to heuristic")
                return False

        return True

    def _known_local_runtime_capability_ids(self) -> set[str]:
        if self._capability_manager is None:
            return set()
        try:
            return {
                str(item.get("capability_id") or "").strip()
                for item in self._capability_manager.list_capabilities()
                if str(item.get("capability_id") or "").strip()
            }
        except Exception:
            logger.warning("Failed to enumerate local runtime capabilities", exc_info=True)
            return set()

    def _heuristic_plan(self, task: str) -> list[TaskStep]:
        hints = self._task_hints(task)
        capability_tools = self._capabilities_for_task(task)

        acquisition_caps = [cap for cap in capability_tools if cap in self._ACQUISITION_CAPS]
        processing_caps = [cap for cap in capability_tools if cap in self._PROCESSING_CAPS]
        delivery_caps = [cap for cap in capability_tools if cap in self._DELIVERY_CAPS]

        steps: list[TaskStep] = []
        if acquisition_caps:
            steps.append(
                TaskStep(
                    step_id="step-1",
                    description="在边缘节点获取交互式或本地资源",
                    required_capabilities=acquisition_caps,
                    preferred_tools=self._tool_names_for_capabilities(acquisition_caps, capability_tools),
                    timeout_seconds=1800.0 if hints["long_running"] else 900.0,
                    metadata={
                        "requires_user_interaction": hints["requires_user_interaction"] or "browser_automation" in acquisition_caps,
                        "preferred_edge": True,
                    },
                )
            )

        if processing_caps:
            steps.append(
                TaskStep(
                    step_id=f"step-{len(steps) + 1}",
                    description="在持久化节点执行分析、整理或入库",
                    required_capabilities=processing_caps,
                    preferred_tools=self._tool_names_for_capabilities(processing_caps, capability_tools),
                    depends_on=[steps[-1].step_id] if steps else [],
                    timeout_seconds=3600.0 if hints["long_running"] else 1200.0,
                    metadata={
                        "long_running": hints["long_running"] or bool({"long_running_analysis", "knowledge_store"} & set(processing_caps)),
                        "preferred_hub": True,
                        "privacy_local_only": hints["privacy_local_only"] or "local_llm_inference" in processing_caps,
                    },
                )
            )

        if delivery_caps:
            steps.append(
                TaskStep(
                    step_id=f"step-{len(steps) + 1}",
                    description="将结果推送到可交互节点",
                    required_capabilities=delivery_caps,
                    preferred_tools=self._tool_names_for_capabilities(delivery_caps, capability_tools),
                    depends_on=[steps[-1].step_id] if steps else [],
                    timeout_seconds=300.0,
                    metadata={"preferred_edge": True},
                )
            )

        if not steps:
            steps.append(
                TaskStep(
                    step_id="step-1",
                    description="在当前节点执行任务",
                    required_capabilities=[],
                    preferred_tools=[],
                    timeout_seconds=1800.0 if hints["long_running"] else 900.0,
                    metadata={
                        "long_running": hints["long_running"],
                        "privacy_local_only": hints["privacy_local_only"],
                    },
                )
            )

        return steps

    def _task_hints(self, task: str) -> dict[str, bool]:
        compact = re.sub(r"\s+", "", task.lower())
        return {
            "long_running": any(token in compact for token in ("长时间", "持续", "不间断", "后台", "批处理", "分析", "整理", "总结", "入库")),
            "requires_user_interaction": any(token in compact for token in ("登录", "扫码", "验证码", "授权", "确认")),
            "privacy_local_only": any(token in compact for token in ("隐私", "敏感", "不出本地", "本地模型", "病历", "健康数据")),
        }

    def _capabilities_for_task(self, task: str) -> dict[str, list[str]]:
        detected: dict[str, list[str]] = {}
        compact = re.sub(r"\s+", "", task.lower())
        for capability_id, keywords, tools in self._CAPABILITY_RULES:
            if any(keyword.lower().replace(" ", "") in compact for keyword in keywords):
                detected[capability_id] = list(tools)

        if any(action in compact for action in self._OPEN_ACTIONS):
            if any(app.lower().replace(" ", "") in compact for app in self._BROWSER_APPS):
                detected.setdefault(
                    "browser_automation",
                    ["browser_navigate", "browser_extract_text", "browser_fill_form", "browser_screenshot"],
                )
            elif any(app.lower().replace(" ", "") in compact for app in self._AUTOMATION_APPS):
                detected.setdefault("apple_automation", ["run_applescript"])

        if (
            "飞书" in compact
            and any(token in compact for token in self._FEISHU_DELIVERY_HINTS)
            and "notifications" not in detected
        ):
            detected["notifications"] = ["send_notification"]
        return detected

    @staticmethod
    def _tool_names_for_capabilities(
        capability_ids: list[str],
        capability_tools: dict[str, list[str]],
    ) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for capability_id in capability_ids:
            for tool_name in capability_tools.get(capability_id, []):
                if tool_name not in seen:
                    seen.add(tool_name)
                    names.append(tool_name)
        return names

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
