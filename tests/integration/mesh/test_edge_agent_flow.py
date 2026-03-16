"""Integration tests for the Phase 1 edge agent flow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nexus.agent.types import ToolDefinition, ToolRiskLevel
from nexus.edge import EdgeNodeAgent, EdgeToolExecutor, build_edge_tool_registry
from nexus.mesh import (
    AvailabilitySpec,
    CapabilitySpec,
    InMemoryTransport,
    MeshRegistry,
    MessageType,
    NodeCard,
    NodeType,
    ProviderSpec,
    RemoteToolProxy,
    ResourceSpec,
    TaskAssignment,
    TaskExecutionResult,
    TaskStepState,
    task_assign_topic,
    task_result_topic,
    task_status_topic,
)
from nexus.services.workspace import WorkspaceService


class _FakeBrowserService:
    enabled = True

    async def navigate(self, url: str) -> dict[str, str]:
        return {"url": url, "title": "Example Domain"}

    async def extract_text(self, selector: str | None = None) -> dict[str, str | None]:
        return {"text": "Example page body", "selector": selector}

    async def screenshot(self, path: str | None = None) -> dict[str, str]:
        return {"path": path or "/tmp/browser-shot.png"}

    async def fill_form(self, fields: dict[str, str]) -> dict[str, object]:
        return {"filled": dict(fields)}


class _FakeCommandRunner:
    def __init__(self) -> None:
        self.clipboard = ""
        self.commands: list[list[str]] = []

    async def __call__(self, args: list[str], stdin: bytes | None = None):
        from nexus.edge.tools import CommandResult

        self.commands.append(list(args))
        if args[0] == "pbcopy":
            self.clipboard = (stdin or b"").decode("utf-8")
            return CommandResult(0, "", "")
        if args[0] == "pbpaste":
            return CommandResult(0, self.clipboard, "")
        if args[0] == "screencapture":
            return CommandResult(0, args[-1], "")
        if args[0] == "shortcuts":
            if len(args) >= 2 and args[1] == "list":
                return CommandResult(0, "Daily Briefing\nInbox Capture\n", "")
            if len(args) >= 3 and args[1] == "run":
                return CommandResult(0, f"shortcut:{args[2]}\n", "")
        if args[0] == "osascript":
            return CommandResult(0, "ok\n", "")
        raise AssertionError(f"Unexpected command: {args}")


class _FlakyInMemoryTransport(InMemoryTransport):
    async def publish(self, topic: str, message):  # type: ignore[override]
        if topic.endswith("/heartbeat") or topic.endswith("/offline"):
            raise RuntimeError("simulated transport failure")
        await super().publish(topic, message)


class _ReconnectOnceTransport(InMemoryTransport):
    def __init__(self, node_id: str, *, hub_name: str) -> None:
        super().__init__(node_id, hub_name=hub_name)
        self.connect_count = 0
        self.disconnect_count = 0
        self.card_publish_count = 0
        self._failed_once = False

    async def connect(self) -> None:  # type: ignore[override]
        self.connect_count += 1
        await super().connect()

    async def disconnect(self) -> None:  # type: ignore[override]
        self.disconnect_count += 1
        await super().disconnect()

    async def publish(self, topic: str, message):  # type: ignore[override]
        if topic.endswith("/card"):
            self.card_publish_count += 1
        if topic.endswith("/heartbeat") and not self._failed_once:
            self._failed_once = True
            self._connected = False
            raise RuntimeError("simulated heartbeat disconnect")
        await super().publish(topic, message)


def _make_hub_card() -> NodeCard:
    return NodeCard(
        node_id="ubuntu-server-5090",
        node_type=NodeType.HUB,
        display_name="Ubuntu Server",
        platform="linux",
        arch="x86_64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=[],
        resources=ResourceSpec(cpu_cores=32, memory_gb=128, gpu="RTX 5090", battery_powered=False),
        availability=AvailabilitySpec(schedule="24/7"),
    )


def _make_edge_card() -> NodeCard:
    return NodeCard(
        node_id="macbook-pro",
        node_type=NodeType.EDGE,
        display_name="MacBook Pro",
        platform="macos",
        arch="arm64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=[
            CapabilitySpec(
                capability_id="browser_automation",
                description="Authenticated browser automation",
                tools=[
                    "browser_navigate",
                    "browser_extract_text",
                    "browser_screenshot",
                    "browser_fill_form",
                ],
                requires_user_interaction=True,
            ),
            CapabilitySpec(
                capability_id="local_filesystem",
                description="Edge-local filesystem access",
                tools=["list_local_files", "code_read_file"],
            ),
            CapabilitySpec(
                capability_id="screen_capture",
                description="Screen capture and recording",
                tools=["capture_screen", "record_screen"],
            ),
            CapabilitySpec(
                capability_id="clipboard",
                description="Clipboard read and write",
                tools=["read_clipboard", "write_clipboard"],
            ),
        ],
        resources=ResourceSpec(cpu_cores=10, memory_gb=16, battery_powered=True),
        availability=AvailabilitySpec(intermittent=True, max_task_duration_seconds=1800),
    )


@pytest.mark.asyncio
async def test_remote_tool_proxy_calls_edge_node_tools(tmp_path: Path):
    InMemoryTransport.reset_hub("phase1-remote")
    hub_transport = InMemoryTransport("ubuntu-server-5090", hub_name="phase1-remote")
    edge_transport = InMemoryTransport("macbook-pro", hub_name="phase1-remote")
    await hub_transport.connect()

    registry = MeshRegistry(hub_transport)
    await registry.setup_transport_handlers()
    await registry.register_node(_make_hub_card())

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "notes.txt").write_text("mesh-phase-1", encoding="utf-8")
    command_runner = _FakeCommandRunner()
    tool_executor = EdgeToolExecutor(
        build_edge_tool_registry(
            workspace_service=WorkspaceService([workspace_root]),
            browser_service=_FakeBrowserService(),
            command_runner=command_runner,
            enable_macos_tools=True,
            scratch_dir=tmp_path / "artifacts",
        )
    )
    edge_agent = EdgeNodeAgent(
        transport=edge_transport,
        tool_executor=tool_executor,
        node_card=_make_edge_card(),
        heartbeat_interval_seconds=0.05,
    )

    try:
        await edge_agent.start()

        proxy = RemoteToolProxy(
            transport=hub_transport,
            registry=registry,
            local_node_id="ubuntu-server-5090",
        )
        tools = {tool.name: tool for tool in proxy.build_remote_tools()}
        browser_tool = RemoteToolProxy.alias_for("macbook-pro", "browser_navigate")
        read_file_tool = RemoteToolProxy.alias_for("macbook-pro", "code_read_file")
        write_clipboard_tool = RemoteToolProxy.alias_for("macbook-pro", "write_clipboard")
        read_clipboard_tool = RemoteToolProxy.alias_for("macbook-pro", "read_clipboard")
        screenshot_tool = RemoteToolProxy.alias_for("macbook-pro", "capture_screen")

        browser_result = json.loads(await tools[browser_tool].handler(url="https://example.com"))
        assert browser_result["title"] == "Example Domain"

        file_result = await tools[read_file_tool].handler(path="notes.txt")
        assert file_result == "mesh-phase-1"

        write_result = json.loads(await tools[write_clipboard_tool].handler(content="copied-text"))
        assert write_result["ok"] is True

        clipboard_result = json.loads(await tools[read_clipboard_tool].handler())
        assert clipboard_result["content"] == "copied-text"

        screenshot_result = json.loads(await tools[screenshot_tool].handler())
        assert screenshot_result["path"].endswith(".png")
        assert any(command[0] == "screencapture" for command in command_runner.commands)
    finally:
        await edge_agent.stop()


@pytest.mark.asyncio
async def test_edge_agent_executes_task_assignment_and_publishes_result(tmp_path: Path):
    InMemoryTransport.reset_hub("phase1-task")
    hub_transport = InMemoryTransport("ubuntu-server-5090", hub_name="phase1-task")
    edge_transport = InMemoryTransport("macbook-pro", hub_name="phase1-task")
    await hub_transport.connect()

    registry = MeshRegistry(hub_transport)
    await registry.setup_transport_handlers()
    await registry.register_node(_make_hub_card())

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    (workspace_root / "task.txt").write_text("task-result", encoding="utf-8")

    edge_agent = EdgeNodeAgent(
        transport=edge_transport,
        tool_executor=EdgeToolExecutor(
            build_edge_tool_registry(
                workspace_service=WorkspaceService([workspace_root]),
                browser_service=_FakeBrowserService(),
                command_runner=_FakeCommandRunner(),
                enable_macos_tools=True,
                scratch_dir=tmp_path / "artifacts",
            )
        ),
        node_card=_make_edge_card(),
        heartbeat_interval_seconds=0.05,
    )

    statuses: list[str] = []
    results: list[TaskExecutionResult] = []

    async def on_status(_topic, message):
        statuses.append(str(message.payload.get("status")))

    async def on_result(_topic, message):
        results.append(TaskExecutionResult.from_dict(message.payload))

    await hub_transport.subscribe(task_status_topic("task-001"), on_status)
    await hub_transport.subscribe(task_result_topic("task-001"), on_result)

    try:
        await edge_agent.start()

        assignment = TaskAssignment(
            task_id="task-001",
            step_id="step-001",
            assigned_node="macbook-pro",
            tool_name="code_read_file",
            arguments={"path": "task.txt"},
        )
        topic = task_assign_topic(assignment.task_id)
        message = hub_transport.make_message(
            MessageType.TASK_ASSIGN,
            topic,
            assignment.to_dict(),
            target_node="macbook-pro",
        )
        await hub_transport.publish(topic, message)

        assert statuses == [TaskStepState.RUNNING.value, TaskStepState.SUCCEEDED.value]
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "task-result"
    finally:
        await edge_agent.stop()


@pytest.mark.asyncio
async def test_edge_agent_republishes_node_card_for_late_joining_hub(tmp_path: Path):
    InMemoryTransport.reset_hub("phase1-reregister")
    edge_transport = InMemoryTransport("macbook-pro", hub_name="phase1-reregister")
    hub_transport = InMemoryTransport("ubuntu-server-5090", hub_name="phase1-reregister")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    edge_agent = EdgeNodeAgent(
        transport=edge_transport,
        tool_executor=EdgeToolExecutor(
            build_edge_tool_registry(
                workspace_service=WorkspaceService([workspace_root]),
                browser_service=_FakeBrowserService(),
                command_runner=_FakeCommandRunner(),
                enable_macos_tools=True,
                scratch_dir=tmp_path / "artifacts",
            )
        ),
        node_card=_make_edge_card(),
        heartbeat_interval_seconds=0.05,
        card_refresh_interval_seconds=0.1,
    )

    try:
        await edge_agent.start()
        await hub_transport.connect()

        registry = MeshRegistry(hub_transport)
        await registry.setup_transport_handlers()
        await registry.register_node(_make_hub_card())

        async def wait_for_edge_node() -> None:
            for _ in range(40):
                card = registry.get_node("macbook-pro")
                status = registry.get_node_status("macbook-pro")
                if card is not None and status is not None and status.online:
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("Hub never rediscovered macbook-pro after late subscription")

        await asyncio.wait_for(wait_for_edge_node(), timeout=1.5)
    finally:
        await edge_agent.stop()
        await hub_transport.disconnect()


@pytest.mark.asyncio
async def test_edge_agent_recovers_transport_and_republishes_node_card(tmp_path: Path):
    InMemoryTransport.reset_hub("phase1-recover")
    hub_transport = InMemoryTransport("ubuntu-server-5090", hub_name="phase1-recover")
    edge_transport = _ReconnectOnceTransport("macbook-pro", hub_name="phase1-recover")
    await hub_transport.connect()

    registry = MeshRegistry(hub_transport)
    await registry.setup_transport_handlers()
    await registry.register_node(_make_hub_card())

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    edge_agent = EdgeNodeAgent(
        transport=edge_transport,
        tool_executor=EdgeToolExecutor(
            build_edge_tool_registry(
                workspace_service=WorkspaceService([workspace_root]),
                browser_service=_FakeBrowserService(),
                command_runner=_FakeCommandRunner(),
                enable_macos_tools=True,
                scratch_dir=tmp_path / "artifacts",
            )
        ),
        node_card=_make_edge_card(),
        heartbeat_interval_seconds=0.01,
        card_refresh_interval_seconds=0.02,
    )

    try:
        await edge_agent.start()

        async def wait_for_recovery() -> None:
            for _ in range(80):
                card = registry.get_node("macbook-pro")
                status = registry.get_node_status("macbook-pro")
                if (
                    card is not None
                    and status is not None
                    and status.online
                    and edge_transport.connect_count >= 2
                    and edge_transport.card_publish_count >= 2
                ):
                    return
                await asyncio.sleep(0.02)
            raise AssertionError(
                f"Recovery did not happen: connects={edge_transport.connect_count} cards={edge_transport.card_publish_count}"
            )

        await asyncio.wait_for(wait_for_recovery(), timeout=2.0)
    finally:
        await edge_agent.stop()
        await hub_transport.disconnect()


@pytest.mark.asyncio
async def test_edge_agent_stop_ignores_heartbeat_publish_failure(tmp_path: Path):
    InMemoryTransport.reset_hub("phase1-shutdown")
    edge_transport = _FlakyInMemoryTransport("macbook-pro", hub_name="phase1-shutdown")

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    edge_agent = EdgeNodeAgent(
        transport=edge_transport,
        tool_executor=EdgeToolExecutor(
            build_edge_tool_registry(
                workspace_service=WorkspaceService([workspace_root]),
                browser_service=_FakeBrowserService(),
                command_runner=_FakeCommandRunner(),
                enable_macos_tools=True,
                scratch_dir=tmp_path / "artifacts",
            )
        ),
        node_card=_make_edge_card(),
        heartbeat_interval_seconds=0.01,
    )

    await edge_agent.start()
    await asyncio.sleep(0.03)
    await edge_agent.stop()


@pytest.mark.asyncio
async def test_edge_agent_waits_for_approval_before_running_task() -> None:
    InMemoryTransport.reset_hub("phase1-approval")
    hub_transport = InMemoryTransport("ubuntu-server-5090", hub_name="phase1-approval")
    edge_transport = InMemoryTransport("macbook-pro", hub_name="phase1-approval")
    await hub_transport.connect()

    registry = MeshRegistry(hub_transport)
    await registry.setup_transport_handlers()
    await registry.register_node(_make_hub_card())

    approval_gate = asyncio.Event()

    async def dangerous_echo(message: str) -> str:
        return message.upper()

    tool_executor = EdgeToolExecutor(
        [
            ToolDefinition(
                name="dangerous_echo",
                description="Echo a message after manual approval.",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                handler=dangerous_echo,
                risk_level=ToolRiskLevel.HIGH,
                requires_approval=True,
                tags=["approval", "test"],
            )
        ]
    )
    edge_card = NodeCard(
        node_id="macbook-pro",
        node_type=NodeType.EDGE,
        display_name="MacBook Pro",
        platform="macos",
        arch="arm64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=[
            CapabilitySpec(
                capability_id="approved_actions",
                description="Manually approved actions",
                tools=["dangerous_echo"],
                requires_user_interaction=True,
            )
        ],
        resources=ResourceSpec(cpu_cores=10, memory_gb=16, battery_powered=True),
        availability=AvailabilitySpec(intermittent=True, max_task_duration_seconds=1800),
    )

    async def approval_handler(_tool, _arguments, _context) -> None:
        await approval_gate.wait()

    edge_agent = EdgeNodeAgent(
        transport=edge_transport,
        tool_executor=tool_executor,
        node_card=edge_card,
        heartbeat_interval_seconds=0.05,
        approval_handler=approval_handler,
    )

    statuses: list[str] = []
    results: list[TaskExecutionResult] = []

    async def on_status(_topic, message):
        statuses.append(str(message.payload.get("status")))

    async def on_result(_topic, message):
        results.append(TaskExecutionResult.from_dict(message.payload))

    await hub_transport.subscribe(task_status_topic("task-approval"), on_status)
    await hub_transport.subscribe(task_result_topic("task-approval"), on_result)

    try:
        await edge_agent.start()

        assignment = TaskAssignment(
            task_id="task-approval",
            step_id="step-approval",
            assigned_node="macbook-pro",
            tool_name="dangerous_echo",
            arguments={"message": "needs approval"},
            timeout_seconds=30.0,
        )
        topic = task_assign_topic(assignment.task_id)
        message = hub_transport.make_message(
            MessageType.TASK_ASSIGN,
            topic,
            assignment.to_dict(),
            target_node="macbook-pro",
        )
        publish_task = asyncio.create_task(hub_transport.publish(topic, message))

        async def wait_for_waiting_state() -> None:
            for _ in range(50):
                if statuses == [TaskStepState.WAITING_APPROVAL.value]:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError(f"Unexpected statuses before approval: {statuses}")

        await asyncio.wait_for(wait_for_waiting_state(), timeout=1.0)
        approval_gate.set()

        async def wait_for_completion() -> None:
            for _ in range(50):
                if statuses[-2:] == [TaskStepState.RUNNING.value, TaskStepState.SUCCEEDED.value]:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError(f"Task never completed: {statuses}")

        await asyncio.wait_for(wait_for_completion(), timeout=1.0)
        await asyncio.wait_for(publish_task, timeout=1.0)
        assert statuses == [
            TaskStepState.WAITING_APPROVAL.value,
            TaskStepState.RUNNING.value,
            TaskStepState.SUCCEEDED.value,
        ]
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].output == "NEEDS APPROVAL"
    finally:
        await edge_agent.stop()
