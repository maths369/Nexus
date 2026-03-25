from __future__ import annotations

import asyncio
import platform
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nexus.agent.types import ToolDefinition, ToolRiskLevel
from nexus.mesh import InMemoryTransport, NodeCard
from nexus.mesh.task_protocol import TaskAssignment, TaskStepState
from nexus.shared import load_nexus_settings
from nexus.edge.agent import ApprovalRequestContext
from nexus.edge.local_runtime import LocalRunResult
from nexus.edge.macos_sidecar import (
    ApprovalManager,
    MacOSSidecarRuntime,
    ObservableEdgeNodeAgent,
    SidecarState,
    _parse_battery_percent,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


class FailingTransport:
    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.connected = False

    async def connect(self) -> None:
        raise RuntimeError("connection refused")

    async def disconnect(self) -> None:
        self.connected = False


def _runtime(root: Path) -> MacOSSidecarRuntime:
    settings = load_nexus_settings(root)
    mesh = {
        "broker_host": "127.0.0.1",
        "broker_port": 1883,
        "transport": "tcp",
        "websocket_path": "/mqtt",
        "username": None,
        "password": None,
        "keepalive_seconds": 60,
        "qos": 1,
        "tls_enabled": False,
        "tls_ca_path": None,
        "tls_cert_path": None,
        "tls_key_path": None,
        "tls_insecure": False,
        "node_card_path": str(root / "config" / "node_cards" / "macbook-pro.example.yaml"),
    }
    node_card = NodeCard.from_yaml_file(mesh["node_card_path"])
    return MacOSSidecarRuntime(
        settings=settings,
        http_host="127.0.0.1",
        http_port=8765,
        mesh_config=mesh,
        transport=InMemoryTransport(node_card.node_id, hub_name="macos-sidecar-test"),
        node_card=node_card,
        browser_enabled=False,
    )


def _runtime_with_transport(root: Path, transport: object) -> MacOSSidecarRuntime:
    settings = load_nexus_settings(root)
    mesh = {
        "broker_host": "127.0.0.1",
        "broker_port": 1883,
        "transport": "tcp",
        "websocket_path": "/mqtt",
        "username": None,
        "password": None,
        "keepalive_seconds": 60,
        "qos": 1,
        "tls_enabled": False,
        "tls_ca_path": None,
        "tls_cert_path": None,
        "tls_key_path": None,
        "tls_insecure": False,
        "node_card_path": str(root / "config" / "node_cards" / "macbook-pro.example.yaml"),
    }
    node_card = NodeCard.from_yaml_file(mesh["node_card_path"])
    return MacOSSidecarRuntime(
        settings=settings,
        http_host="127.0.0.1",
        http_port=8765,
        mesh_config=mesh,
        transport=transport,
        node_card=node_card,
        browser_enabled=False,
    )

@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sidecar runtime tests require Darwin")
def test_sidecar_health_and_status() -> None:
    root = REPO_ROOT
    runtime = _runtime(root)
    app = runtime.app()

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        status = client.get("/status")
        payload = status.json()
        assert payload["phase"] == "running"
        assert payload["transport_connected"] is True
        assert payload["hub"]["api_host"] == "127.0.0.1"
        assert payload["hub"]["api_port"] == 8000
        assert payload["hub"]["connectivity_state"] in {"connected", "broker_only", "local_only"}
        assert payload["node_card"]["platform"] == "macos"
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert "list_local_files" in tool_names
        assert "capture_screen" in tool_names

        events = client.get("/events")
        assert events.status_code == 200
        assert events.json()["events"]

@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sidecar runtime tests require Darwin")
def test_sidecar_reconciles_missing_browser_tools() -> None:
    root = REPO_ROOT
    runtime = _runtime(root)
    app = runtime.app()

    with TestClient(app) as client:
        node_card = client.get("/node-card").json()["node_card"]
        capability_ids = {cap["capability_id"] for cap in node_card["capabilities"]}
        assert "browser_automation" not in capability_ids


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sidecar runtime tests require Darwin")
def test_sidecar_runs_local_only_when_mesh_unavailable() -> None:
    root = REPO_ROOT
    failing_transport = FailingTransport("macbook-pro-test")
    runtime = _runtime_with_transport(root, failing_transport)
    app = runtime.app()

    with TestClient(app) as client:
        status = client.get("/status")
        payload = status.json()
        assert payload["phase"] == "running"
        assert payload["transport_connected"] is False
        assert payload["hub"]["connectivity_state"] in {"local_only", "reconnecting"}
        assert payload["node_card"]["platform"] == "macos"
        assert "Hub unavailable" in (payload["last_error"] or "")
        assert any(event["message"] == "Hub unavailable, running local only" for event in payload["recent_events"])


def test_parse_battery_percent() -> None:
    assert _parse_battery_percent("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234567)\t84%;") == 84.0
    assert _parse_battery_percent("no battery here") is None


@pytest.mark.asyncio
async def test_approval_manager_round_trip() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    manager = ApprovalManager(state)
    async def _noop_handler(**_: object) -> str:
        return "ok"

    tool = ToolDefinition(
        name="run_applescript",
        description="Run AppleScript",
        parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
        handler=_noop_handler,
        risk_level=ToolRiskLevel.CRITICAL,
        requires_approval=True,
    )

    request_task = asyncio.create_task(
        manager.request(
            tool,
            {"script": "display dialog \"hello\""},
            ApprovalRequestContext(source="task", task_id="task-1", step_id="step-1", timeout_seconds=5.0),
        )
    )

    for _ in range(20):
        if state.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert len(state.pending_approvals) == 1
    approval_id = next(iter(state.pending_approvals))

    approval = await manager.resolve(approval_id, approved=True, comment="allowed")
    assert approval.tool_name == "run_applescript"

    await request_task
    assert not state.pending_approvals
    assert any(event.kind == "approval_granted" for event in state.recent_events)


@pytest.mark.asyncio
async def test_runtime_wrapped_tool_uses_approval_manager() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    manager = ApprovalManager(state)

    class FakeExecutor:
        async def execute(self, tool_name: str, arguments: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "output": f"{tool_name}:{arguments['script']}",
                    "error": None,
                },
            )()

    runtime = object.__new__(MacOSSidecarRuntime)
    runtime._approval_manager = manager
    runtime._tool_executor = FakeExecutor()

    async def _noop_handler(**_: object) -> str:
        return "noop"

    tool = ToolDefinition(
        name="run_applescript",
        description="Run AppleScript",
        parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
        handler=_noop_handler,
        risk_level=ToolRiskLevel.CRITICAL,
        requires_approval=True,
    )

    wrapped = runtime._wrap_runtime_tool(tool)
    task = asyncio.create_task(wrapped.handler(script='tell application "Google Chrome" to activate'))

    for _ in range(20):
        if state.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert len(state.pending_approvals) == 1

    approval_id = next(iter(state.pending_approvals))
    await manager.resolve(approval_id, approved=True, comment="allowed")

    output = await task
    assert "Google Chrome" in output


@pytest.mark.asyncio
async def test_execute_local_fast_path_opens_chrome_via_applescript() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    manager = ApprovalManager(state)

    class FakeExecutor:
        async def execute(self, tool_name: str, arguments: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "output": arguments["script"],
                    "error": None,
                },
            )()

    runtime = object.__new__(MacOSSidecarRuntime)
    runtime._approval_manager = manager
    runtime._tool_executor = FakeExecutor()

    async def _noop_handler(**_: object) -> str:
        return "noop"

    wrapped = runtime._wrap_runtime_tool(
        ToolDefinition(
            name="run_applescript",
            description="Run AppleScript",
            parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
            handler=_noop_handler,
            risk_level=ToolRiskLevel.CRITICAL,
            requires_approval=True,
        )
    )
    runtime._runtime_tools_by_name = {"run_applescript": wrapped}

    task = asyncio.create_task(runtime.execute_local_command("打开Chrome"))
    for _ in range(20):
        if state.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert len(state.pending_approvals) == 1

    approval_id = next(iter(state.pending_approvals))
    await manager.resolve(approval_id, approved=True, comment="allowed")

    result = await task
    assert result.success is True
    assert result.model == "fast-path"
    assert "Google Chrome" in result.output


@pytest.mark.asyncio
async def test_execute_delegated_command_uses_fast_path_for_open_app() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    manager = ApprovalManager(state)

    class FakeExecutor:
        async def execute(self, tool_name: str, arguments: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "output": arguments["script"],
                    "error": None,
                },
            )()

    runtime = object.__new__(MacOSSidecarRuntime)
    runtime._approval_manager = manager
    runtime._tool_executor = FakeExecutor()
    runtime._state = state

    async def _noop_handler(**_: object) -> str:
        return "noop"

    wrapped = runtime._wrap_runtime_tool(
        ToolDefinition(
            name="run_applescript",
            description="Run AppleScript",
            parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
            handler=_noop_handler,
            risk_level=ToolRiskLevel.CRITICAL,
            requires_approval=True,
        )
    )
    runtime._runtime_tools_by_name = {"run_applescript": wrapped}
    runtime._edge_runtime = None

    task = asyncio.create_task(runtime.execute_delegated_command("打开Chrome"))
    for _ in range(20):
        if state.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert len(state.pending_approvals) == 1

    approval_id = next(iter(state.pending_approvals))
    await manager.resolve(approval_id, approved=True, comment="allowed")

    result = await task
    assert result.success is True
    assert result.model == "fast-path"
    assert "Google Chrome" in result.output


@pytest.mark.asyncio
async def test_execute_delegated_command_normalizes_mac_context_and_browser_suffix() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    manager = ApprovalManager(state)

    class FakeExecutor:
        async def execute(self, tool_name: str, arguments: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "output": arguments["script"],
                    "error": None,
                },
            )()

    runtime = object.__new__(MacOSSidecarRuntime)
    runtime._approval_manager = manager
    runtime._tool_executor = FakeExecutor()
    runtime._state = state

    async def _noop_handler(**_: object) -> str:
        return "noop"

    wrapped = runtime._wrap_runtime_tool(
        ToolDefinition(
            name="run_applescript",
            description="Run AppleScript",
            parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
            handler=_noop_handler,
            risk_level=ToolRiskLevel.CRITICAL,
            requires_approval=True,
        )
    )
    runtime._runtime_tools_by_name = {"run_applescript": wrapped}
    runtime._edge_runtime = None

    task = asyncio.create_task(runtime.execute_delegated_command("你打开MacBook Pro上的Chrome浏览器"))
    for _ in range(20):
        if state.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert len(state.pending_approvals) == 1

    approval_id = next(iter(state.pending_approvals))
    await manager.resolve(approval_id, approved=True, comment="allowed")

    result = await task
    assert result.success is True
    assert result.model == "fast-path"
    assert "Google Chrome" in result.output
    assert "make new window" in result.output


@pytest.mark.asyncio
async def test_execute_delegated_command_uses_browser_handoff_for_known_site() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    manager = ApprovalManager(state)

    class FakeExecutor:
        async def execute(self, tool_name: str, arguments: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "output": arguments["script"],
                    "error": None,
                },
            )()

    runtime = object.__new__(MacOSSidecarRuntime)
    runtime._approval_manager = manager
    runtime._tool_executor = FakeExecutor()
    runtime._state = state

    async def _noop_handler(**_: object) -> str:
        return "noop"

    wrapped = runtime._wrap_runtime_tool(
        ToolDefinition(
            name="run_applescript",
            description="Run AppleScript",
            parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
            handler=_noop_handler,
            risk_level=ToolRiskLevel.CRITICAL,
            requires_approval=True,
        )
    )
    runtime._runtime_tools_by_name = {"run_applescript": wrapped}
    runtime._edge_runtime = None

    task = asyncio.create_task(runtime.execute_delegated_command("你在MacBook Pro上的Chrome打开网易邮箱"))
    for _ in range(20):
        if state.pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert len(state.pending_approvals) == 1

    approval_id = next(iter(state.pending_approvals))
    await manager.resolve(approval_id, approved=True, comment="allowed")

    result = await task
    assert result.success is True
    assert result.model == "fast-path"
    assert "mail.163.com" in result.output
    assert 'set URL of active tab of front window to "https://mail.163.com"' in result.output


@pytest.mark.asyncio
async def test_observable_edge_agent_uses_delegated_executor_before_edge_llm() -> None:
    state = SidecarState(
        root_dir=Path("/tmp"),
        http_host="127.0.0.1",
        http_port=8765,
        tools=[],
        mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
        browser_enabled=False,
    )
    agent = object.__new__(ObservableEdgeNodeAgent)
    agent._sidecar_state = state
    agent._active_executions = 0

    calls: list[tuple[str, dict[str, object] | None]] = []
    statuses: list[tuple[TaskStepState, str | None]] = []
    results: list[object] = []

    async def delegated_executor(task: str, constraints: dict[str, object] | None) -> LocalRunResult:
        calls.append((task, constraints))
        return LocalRunResult(
            run_id="fast-delegated",
            task=task,
            success=True,
            output="已在这台 Mac 上打开 Google Chrome。",
            duration_ms=12.0,
            model="fast-path",
        )

    async def publish_status(assignment: TaskAssignment, state_value: TaskStepState, *, error: str | None = None) -> None:
        statuses.append((state_value, error))

    async def publish_result(assignment: TaskAssignment, result: object) -> None:
        results.append(result)

    agent._delegated_executor = delegated_executor
    agent._publish_task_status = publish_status
    agent._publish_task_result = publish_result

    assignment = TaskAssignment(
        task_id="task-1",
        step_id="step-1",
        assigned_node="macbook-pro-yanglei",
        tool_name="agent_loop",
        metadata={
            "execution_mode": "agent_loop",
            "task_description": "打开Chrome浏览器",
        },
    )

    await agent._execute_agent_loop(assignment)

    assert calls == [("打开Chrome浏览器", None)]
    assert statuses[0][0] == TaskStepState.RUNNING
    assert statuses[-1][0] == TaskStepState.SUCCEEDED
    assert len(results) == 1
    assert results[0].success is True
    assert "Google Chrome" in results[0].output


@pytest.mark.asyncio
async def test_runtime_testing_mode_skips_local_approval() -> None:
    class FakeExecutor:
        async def execute(self, tool_name: str, arguments: dict[str, object]) -> object:
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "output": f"{tool_name}:{arguments['script']}",
                    "error": None,
                },
            )()

    runtime = object.__new__(MacOSSidecarRuntime)
    runtime._disable_risk_controls = True
    runtime._approval_manager = ApprovalManager(
        SidecarState(
            root_dir=Path("/tmp"),
            http_host="127.0.0.1",
            http_port=8765,
            tools=[],
            mesh_summary={"broker_host": "127.0.0.1", "broker_port": 1883, "transport": "tcp"},
            browser_enabled=False,
        )
    )
    runtime._tool_executor = FakeExecutor()
    runtime._state = runtime._approval_manager._state  # noqa: SLF001

    async def _noop_handler(**_: object) -> str:
        return "noop"

    wrapped = runtime._wrap_runtime_tool(
        ToolDefinition(
            name="run_applescript",
            description="Run AppleScript",
            parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
            handler=_noop_handler,
            risk_level=ToolRiskLevel.CRITICAL,
            requires_approval=False,
        )
    )

    output = await wrapped.handler(script='tell application "Google Chrome" to activate')
    assert "Google Chrome" in output
    assert runtime._state.pending_approvals == {}
