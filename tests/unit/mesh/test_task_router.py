"""Tests for mesh-aware task routing."""

from __future__ import annotations

import pytest

from nexus.mesh import (
    AvailabilitySpec,
    CapabilitySpec,
    InMemoryTransport,
    MeshRegistry,
    NodeCard,
    NodeType,
    ProviderSpec,
    ResourceSpec,
    RemoteToolProxy,
    StepState,
    TaskRouter,
)


class _FakePlannerProvider:
    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, **_: object) -> str:
        return self._response


class _FakeChangeResult:
    def __init__(self, success: bool, reason: str):
        self.success = success
        self.reason = reason


class _FakeCapabilityManager:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = {str(item["capability_id"]): dict(item) for item in items}
        self.enable_calls: list[str] = []

    def list_capabilities(self) -> list[dict[str, object]]:
        return [dict(item) for item in self._items.values()]

    def get_status(self, capability_id: str) -> dict[str, object]:
        item = self._items.get(capability_id)
        if item is None:
            return {
                "capability_id": capability_id,
                "known": False,
                "enabled": False,
                "tools": [],
                "skill_hint": "",
            }
        payload = dict(item)
        payload["known"] = True
        return payload

    async def enable(self, capability_id: str, *, actor: str = "system") -> _FakeChangeResult:
        self.enable_calls.append(capability_id)
        item = self._items.get(capability_id)
        if item is None:
            return _FakeChangeResult(False, f"Unknown capability: {capability_id}")
        item["enabled"] = True
        return _FakeChangeResult(True, "Capability enabled")


def _hub_card() -> NodeCard:
    return NodeCard(
        node_id="ubuntu-server-5090",
        node_type=NodeType.HUB,
        display_name="Ubuntu Server",
        platform="linux",
        arch="x86_64",
        providers=[
            ProviderSpec(name="kimi", model="kimi-k2.5", via="api"),
            ProviderSpec(name="ollama-qwen", model="qwen2.5:72b", via="local"),
        ],
        capabilities=[
            CapabilitySpec(
                capability_id="knowledge_store",
                description="Knowledge base storage",
                tools=["read_vault", "write_vault", "search_vault", "knowledge_ingest"],
            ),
            CapabilitySpec(
                capability_id="long_running_analysis",
                description="Long running processing",
                tools=["background_run"],
            ),
            CapabilitySpec(
                capability_id="local_llm_inference",
                description="Local LLM",
                tools=["local_llm_generate"],
                exclusive=True,
            ),
        ],
        resources=ResourceSpec(cpu_cores=32, memory_gb=128, gpu="RTX 5090", battery_powered=False),
        availability=AvailabilitySpec(schedule="24/7"),
    )


def _edge_card(node_id: str = "macbook-pro", *, load_capable: bool = True) -> NodeCard:
    capabilities = [
        CapabilitySpec(
            capability_id="browser_automation",
            description="Authenticated browser automation",
            tools=["browser_navigate", "browser_extract_text", "browser_fill_form", "browser_screenshot"],
            requires_user_interaction=True,
        ),
    ]
    if load_capable:
        capabilities.append(
            CapabilitySpec(
                capability_id="local_filesystem",
                description="Local filesystem",
                tools=["list_local_files", "code_read_file"],
            )
        )
    return NodeCard(
        node_id=node_id,
        node_type=NodeType.EDGE,
        display_name=f"MacBook {node_id}",
        platform="macos",
        arch="arm64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=capabilities,
        resources=ResourceSpec(cpu_cores=10, memory_gb=16, battery_powered=True),
        availability=AvailabilitySpec(intermittent=True, max_task_duration_seconds=1800),
    )


@pytest.mark.asyncio
async def test_task_router_plans_browser_then_analysis_across_nodes():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-plan")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={
            "read_vault",
            "write_vault",
            "search_vault",
            "background_run",
            "browser_navigate",
            "browser_extract_text",
        },
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-1",
        task="打开浏览器登录网站抓取内容，然后长时间分析整理并入库",
        context_messages=[],
    )

    assert routing.plan.state.value == "ready"
    assert [step.assigned_node for step in routing.plan.steps] == ["macbook-pro", "ubuntu-server-5090"]
    assert routing.extra_tools
    extra_names = {tool.name for tool in routing.extra_tools}
    # Browser step on edge node now uses dispatch tool (agent_loop) instead of individual mesh__ tools
    dispatch_alias = RemoteToolProxy.dispatch_alias_for("macbook-pro")
    assert dispatch_alias in extra_names
    # Individual mesh__ browser tools should NOT be present (they're replaced by dispatch)
    assert RemoteToolProxy.alias_for("macbook-pro", "browser_navigate") not in extra_names
    assert "Mesh 执行上下文" in routing.effective_task


@pytest.mark.asyncio
async def test_task_router_waits_for_missing_browser_node_and_recovers_when_online():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-wait")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run", "system_run"},
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-2",
        task="打开浏览器抓取网页内容",
        context_messages=[],
    )

    assert routing.blocked_reason is not None
    assert routing.plan.steps[0].state == StepState.WAITING_FOR_NODE

    await registry.register_node(_edge_card())
    await router.handle_node_online("macbook-pro")
    plan = router.get_session_plan("session-2")
    assert plan is not None
    assert plan.steps[0].assigned_node == "macbook-pro"
    assert plan.steps[0].state == StepState.ASSIGNED


@pytest.mark.asyncio
async def test_task_router_routes_open_chrome_to_edge_agent_loop():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-open-chrome")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run", "system_run"},
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-open-chrome",
        task="打开Chrome",
        context_messages=[],
    )

    assert routing.plan.steps
    assert routing.plan.steps[0].assigned_node == "macbook-pro"
    assert routing.plan.steps[0].metadata.get("execution_mode") == "agent_loop"
    assert RemoteToolProxy.dispatch_alias_for("macbook-pro") in {tool.name for tool in routing.extra_tools}
    assert "system_run" in routing.disabled_local_tools


@pytest.mark.asyncio
async def test_task_router_augmented_task_includes_live_mesh_context():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-mesh-context")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run", "system_run"},
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-node-capabilities",
        task="你能看到你的MacBook Pro节点能干什么",
        context_messages=[],
    )

    assert "## 当前网络节点" in routing.effective_task
    assert "MacBook macbook-pro" in routing.effective_task
    assert "browser_automation" in routing.effective_task
    assert "local_filesystem" in routing.effective_task


@pytest.mark.asyncio
async def test_task_router_does_not_require_notifications_for_general_feishu_question():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-feishu-general")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run", "system_run"},
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-feishu-general",
        task="你链接到飞书的方式，是否支持你同时连接多个人？",
        context_messages=[],
    )

    assert routing.blocked_reason is None
    assert all("notifications" not in step.required_capabilities for step in routing.plan.steps)


@pytest.mark.asyncio
async def test_task_router_requires_notifications_for_explicit_feishu_delivery():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-feishu-delivery")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run", "system_run"},
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-feishu-delivery",
        task="把结果发到飞书通知我",
        context_messages=[],
    )

    assert routing.blocked_reason is not None
    assert any("notifications" in step.required_capabilities for step in routing.plan.steps)


@pytest.mark.asyncio
async def test_task_router_falls_back_when_provider_planner_refuses_local_task():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-provider-fallback")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    provider = _FakePlannerProvider(
        """{
          "steps": [
            {
              "description": "当前网络中不存在MacBook节点或具备macOS远程控制能力的节点，无法执行打开MacBook Chrome的操作。",
              "required_capabilities": [],
              "preferred_tools": [],
              "metadata": {}
            }
          ]
        }"""
    )

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run"},
        provider=provider,  # type: ignore[arg-type]
        planner_mode="auto",
    )

    routing = await router.prepare_run(
        session_id="session-provider-fallback",
        task="你现在试试打开MacBook上的Chrome",
        context_messages=[],
    )

    assert routing.plan.steps
    assert routing.plan.steps[0].assigned_node == "macbook-pro"
    assert routing.plan.steps[0].metadata.get("execution_mode") == "agent_loop"


@pytest.mark.asyncio
async def test_task_router_accepts_provider_plan_when_it_keeps_required_capability():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-provider-accept")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    provider = _FakePlannerProvider(
        """{
          "steps": [
            {
              "description": "在MacBook上启动Chrome浏览器，如已运行则激活窗口。",
              "required_capabilities": ["browser_automation"],
              "preferred_tools": ["browser_navigate"],
              "metadata": {"requires_user_interaction": true}
            }
          ]
        }"""
    )

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run"},
        provider=provider,  # type: ignore[arg-type]
        planner_mode="auto",
    )

    routing = await router.prepare_run(
        session_id="session-provider-accept",
        task="打开Chrome",
        context_messages=[],
    )

    assert routing.plan.steps
    assert routing.plan.steps[0].description == "在MacBook上启动Chrome浏览器，如已运行则激活窗口。"
    assert routing.plan.steps[0].assigned_node == "macbook-pro"


@pytest.mark.asyncio
async def test_task_router_reroutes_when_primary_edge_goes_offline():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-reroute")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card("macbook-pro-a"))
    await registry.register_node(_edge_card("macbook-pro-b"))
    await registry.heartbeat("macbook-pro-a", current_load=0.1, active_tasks=1)
    await registry.heartbeat("macbook-pro-b", current_load=0.8, active_tasks=3)

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run"},
        planner_mode="heuristic",
    )

    routing = await router.prepare_run(
        session_id="session-3",
        task="打开浏览器抓取网页内容",
        context_messages=[],
    )
    assert routing.plan.steps[0].assigned_node == "macbook-pro-a"

    status = registry.get_node_status("macbook-pro-a")
    assert status is not None
    status.online = False
    updated = await router.handle_node_offline("macbook-pro-a")

    assert updated == ["session-3"]
    plan = router.get_session_plan("session-3")
    assert plan is not None
    assert plan.steps[0].assigned_node == "macbook-pro-b"
    assert plan.steps[0].metadata["rerouted_from"] == "macbook-pro-a"


@pytest.mark.asyncio
async def test_task_router_auto_enables_local_runtime_capability_when_mesh_lacks_it():
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-local-capability")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())

    provider = _FakePlannerProvider(
        """{
          "steps": [
            {
              "description": "将 Excel 文件转换为 CSV。",
              "required_capabilities": ["excel_processing"],
              "preferred_tools": [],
              "metadata": {}
            }
          ]
        }"""
    )
    capability_manager = _FakeCapabilityManager([
        {
            "capability_id": "excel_processing",
            "name": "Excel Processing",
            "description": "支持读取 Excel 工作簿并转换为 CSV。",
            "enabled": False,
            "tools": ["excel_list_sheets", "excel_to_csv"],
            "skill_hint": "excel-processing",
        }
    ])

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run", "excel_list_sheets", "excel_to_csv"},
        capability_manager=capability_manager,
        provider=provider,  # type: ignore[arg-type]
        planner_mode="auto",
    )

    routing = await router.prepare_run(
        session_id="session-local-capability",
        task="把 Excel 转成 CSV",
        context_messages=[],
    )

    assert routing.blocked_reason is None
    assert routing.plan.steps[0].assigned_node == "ubuntu-server-5090"
    assert "excel_processing" in routing.plan.steps[0].metadata.get("auto_local_capabilities", [])
    assert capability_manager.enable_calls == ["excel_processing"]
