"""Tests for mesh-aware task routing (LLM-driven routing)."""

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


# ── Deleted tests (heuristic / provider planner functionality removed) ──
#
# - test_task_router_plans_browser_then_analysis_across_nodes
#     Relied on heuristic multi-step planning (browser_automation → analysis).
#
# - test_task_router_waits_for_missing_browser_node_and_recovers_when_online
#     Relied on heuristic routing detecting browser keywords and creating
#     WAITING_FOR_NODE steps.
#
# - test_task_router_routes_open_chrome_to_edge_agent_loop
#     Relied on heuristic keyword detection ("打开 Chrome") to route to edge.
#
# - test_task_router_requires_notifications_for_explicit_feishu_delivery
#     Relied on heuristic feishu/notification capability detection.
#
# - test_task_router_falls_back_when_provider_planner_refuses_local_task
#     Relied on provider planner + heuristic fallback.
#
# - test_task_router_accepts_provider_plan_when_it_keeps_required_capability
#     Relied on provider planner generating plans with capabilities.
#
# - test_task_router_auto_enables_local_runtime_capability_when_mesh_lacks_it
#     Relied on provider planner generating steps with required_capabilities
#     that triggered auto-enable via capability_manager.


@pytest.mark.asyncio
async def test_prepare_run_creates_single_step_local_plan():
    """LLM-driven routing: prepare_run always creates a single-step plan assigned to hub."""
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-single-step")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "write_vault", "background_run"},
    )

    routing = await router.prepare_run(
        session_id="session-single",
        task="打开浏览器登录网站抓取内容，然后长时间分析整理并入库",
        context_messages=[],
    )

    # Single step, assigned to local hub
    assert len(routing.plan.steps) == 1
    assert routing.plan.steps[0].assigned_node == "ubuntu-server-5090"
    assert routing.plan.state.value == "ready"
    assert routing.blocked_reason is None
    # Dispatch tools for online edge nodes should be injected
    assert routing.extra_tools
    dispatch_alias = RemoteToolProxy.dispatch_alias_for("macbook-pro")
    assert dispatch_alias in {tool.name for tool in routing.extra_tools}
    # No local tools disabled in LLM-driven mode
    assert routing.disabled_local_tools == []


@pytest.mark.asyncio
async def test_task_router_augmented_task_includes_live_mesh_context():
    """effective_task should include edge node info for LLM decision-making."""
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
    )

    routing = await router.prepare_run(
        session_id="session-node-capabilities",
        task="你能看到你的MacBook Pro节点能干什么",
        context_messages=[],
    )

    # _augment_task adds mesh context sections
    assert "Mesh 执行上下文" in routing.effective_task or "可用的边缘节点" in routing.effective_task
    assert "MacBook macbook-pro" in routing.effective_task
    assert "browser_automation" in routing.effective_task
    assert "local_filesystem" in routing.effective_task


@pytest.mark.asyncio
async def test_task_router_does_not_block_for_general_feishu_question():
    """General questions should never be blocked — LLM handles routing."""
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
    )

    routing = await router.prepare_run(
        session_id="session-feishu-general",
        task="你链接到飞书的方式，是否支持你同时连接多个人？",
        context_messages=[],
    )

    assert routing.blocked_reason is None
    assert len(routing.plan.steps) == 1
    assert routing.plan.steps[0].assigned_node == "ubuntu-server-5090"


@pytest.mark.asyncio
async def test_task_router_reroutes_when_primary_edge_goes_offline():
    """handle_node_offline should reroute steps assigned to the offline node."""
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
    )

    # Use plan_task + assign_step to create a plan with an edge-assigned step,
    # since prepare_run now always assigns to local hub.
    from nexus.mesh.task_router import TaskStep, StepState as SS

    plan = await router.plan_task(
        session_id="session-3",
        task="打开浏览器抓取网页内容",
        context=[],
    )
    # Manually add a step with required capabilities to force edge assignment
    edge_step = TaskStep(
        step_id="step-edge",
        description="浏览器抓取",
        required_capabilities=["browser_automation"],
    )
    plan.steps = [edge_step]
    await router.assign_step(edge_step)
    assert edge_step.assigned_node == "macbook-pro-a"

    # Take macbook-pro-a offline
    status = registry.get_node_status("macbook-pro-a")
    assert status is not None
    status.online = False
    updated = await router.handle_node_offline("macbook-pro-a")

    assert updated == ["session-3"]
    rerouted_plan = router.get_session_plan("session-3")
    assert rerouted_plan is not None
    assert rerouted_plan.steps[0].assigned_node == "macbook-pro-b"
    assert rerouted_plan.steps[0].metadata["rerouted_from"] == "macbook-pro-a"


@pytest.mark.asyncio
async def test_prepare_run_no_extra_tools_without_edge_nodes():
    """When no edge nodes are online, no dispatch tools should be injected."""
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-no-edge")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run"},
    )

    routing = await router.prepare_run(
        session_id="session-no-edge",
        task="分析一下数据",
        context_messages=[],
    )

    assert routing.blocked_reason is None
    assert len(routing.plan.steps) == 1
    assert routing.extra_tools == []


@pytest.mark.asyncio
async def test_assign_step_routes_capability_to_edge():
    """assign_step should route steps with edge capabilities to edge nodes."""
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="task-router-assign")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(_hub_card())
    await registry.register_node(_edge_card())

    router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "background_run"},
    )

    from nexus.mesh.task_router import TaskStep

    step = TaskStep(
        step_id="step-browser",
        description="Browser task",
        required_capabilities=["browser_automation"],
    )
    node_id = await router.assign_step(step)
    assert node_id == "macbook-pro"
    assert step.state == StepState.ASSIGNED
    assert step.metadata.get("execution_mode") == "agent_loop"
