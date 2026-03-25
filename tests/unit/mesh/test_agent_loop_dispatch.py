"""Tests for agent-loop dispatch: TaskRouter execution_mode, RemoteToolProxy dispatch, EdgeJournalStore."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.mesh import (
    AvailabilitySpec,
    CapabilitySpec,
    InMemoryTransport,
    MeshMessage,
    MeshRegistry,
    MessageType,
    NodeCard,
    NodeType,
    ProviderSpec,
    RemoteToolProxy,
    ResourceSpec,
    StepState,
    TaskRouter,
    TaskStep,
)
from nexus.mesh.edge_journal_store import EdgeJournalStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hub_card() -> NodeCard:
    return NodeCard(
        node_id="ubuntu-hub",
        node_type=NodeType.HUB,
        display_name="Ubuntu Hub",
        platform="linux",
        capabilities=[
            CapabilitySpec(
                capability_id="knowledge_store",
                description="Knowledge storage",
                tools=["read_vault", "write_vault"],
            ),
        ],
        resources=ResourceSpec(battery_powered=False),
    )


def _edge_card(node_id: str = "macbook-pro") -> NodeCard:
    return NodeCard(
        node_id=node_id,
        node_type=NodeType.EDGE,
        display_name=f"MacBook {node_id}",
        platform="macos",
        capabilities=[
            CapabilitySpec(
                capability_id="browser_automation",
                description="Browser automation",
                tools=["browser_navigate", "browser_extract_text", "browser_fill_form"],
                requires_user_interaction=True,
            ),
            CapabilitySpec(
                capability_id="screen_capture",
                description="Screen capture",
                tools=["capture_screen"],
            ),
            CapabilitySpec(
                capability_id="local_filesystem",
                description="Local filesystem",
                tools=["list_local_files", "code_read_file"],
            ),
        ],
        resources=ResourceSpec(battery_powered=True),
    )


async def _setup_router(
    *,
    hub_card: NodeCard | None = None,
    edge_card: NodeCard | None = None,
) -> tuple[TaskRouter, MeshRegistry, InMemoryTransport]:
    hub = hub_card or _hub_card()
    edge = edge_card or _edge_card()

    transport = InMemoryTransport(hub.node_id, hub_name="agent-loop-test")
    await transport.connect()
    registry = MeshRegistry()
    await registry.register_node(hub)
    await registry.register_node(edge)

    router = TaskRouter(
        registry=registry,
        local_node_id=hub.node_id,
        transport=transport,
        local_tool_names={"read_vault", "write_vault"},
    )
    return router, registry, transport


# ---------------------------------------------------------------------------
# TaskRouter: execution_mode assignment
# ---------------------------------------------------------------------------


class TestTaskRouterAgentLoop:
    @pytest.mark.asyncio
    async def test_plan_task_creates_single_local_step(self) -> None:
        """plan_task (LLM-driven) creates a single local step — no heuristic routing."""
        router, _, _ = await _setup_router()
        plan = await router.plan_task(
            session_id="s1",
            task="请在MacBook上打开浏览器抓取网页内容",
            context=[],
        )
        assert len(plan.steps) == 1
        assert plan.steps[0].assigned_node == "ubuntu-hub"

    @pytest.mark.asyncio
    async def test_multi_cap_step_gets_agent_loop(self) -> None:
        """Steps with 2+ capabilities on edge nodes should get agent_loop."""
        router, _, _ = await _setup_router()
        step = TaskStep(
            step_id="test-step",
            description="Browser and screen",
            required_capabilities=["browser_automation", "screen_capture"],
        )
        await router.assign_step(step)
        assert step.metadata.get("execution_mode") == "agent_loop"
        assert step.assigned_node == "macbook-pro"

    @pytest.mark.asyncio
    async def test_hub_step_does_not_get_agent_loop(self) -> None:
        """Steps assigned to the hub should NOT get execution_mode."""
        router, _, _ = await _setup_router()
        step = TaskStep(
            step_id="hub-step",
            description="Knowledge store task",
            required_capabilities=["knowledge_store"],
        )
        await router.assign_step(step)
        assert step.assigned_node == "ubuntu-hub"
        assert step.metadata.get("execution_mode") is None

    @pytest.mark.asyncio
    async def test_get_agent_loop_steps_filters_correctly(self) -> None:
        """get_agent_loop_steps returns only agent-loop steps assigned to remote nodes."""
        router, _, _ = await _setup_router()
        # 手动构造 agent-loop step
        step = TaskStep(
            step_id="test-step",
            description="Browser task",
            required_capabilities=["browser_automation"],
        )
        await router.assign_step(step)
        from nexus.mesh.task_router import TaskPlan
        plan = TaskPlan(
            task_id="test-plan",
            session_id="s2",
            user_task="test",
            steps=[step],
        )
        agent_steps = router.get_agent_loop_steps(plan)
        for s in agent_steps:
            assert s.metadata["execution_mode"] == "agent_loop"
            assert s.assigned_node != "ubuntu-hub"

    @pytest.mark.asyncio
    async def test_should_use_agent_loop_single_browser_cap(self) -> None:
        """Single browser_automation cap should trigger agent_loop."""
        router, _, _ = await _setup_router()
        step = TaskStep(
            step_id="s1",
            description="Browse",
            required_capabilities=["browser_automation"],
        )
        assert router._should_use_agent_loop(step) is True

    @pytest.mark.asyncio
    async def test_should_not_use_agent_loop_for_notifications(self) -> None:
        """Non-agent-loop caps (e.g. notifications) should NOT trigger."""
        router, _, _ = await _setup_router()
        step = TaskStep(
            step_id="s1",
            description="Notify",
            required_capabilities=["notifications"],
        )
        assert router._should_use_agent_loop(step) is False


# ---------------------------------------------------------------------------
# RemoteToolProxy: dispatch tools
# ---------------------------------------------------------------------------


class TestRemoteToolProxyDispatch:
    @pytest.mark.asyncio
    async def test_build_dispatch_tools(self) -> None:
        """build_dispatch_tools should create dispatch tools for edge nodes."""
        _, registry, transport = await _setup_router()
        proxy = RemoteToolProxy(
            transport=transport,
            registry=registry,
            local_node_id="ubuntu-hub",
        )
        dispatch_tools = proxy.build_dispatch_tools()
        assert len(dispatch_tools) == 1
        tool = dispatch_tools[0]
        assert tool.name == proxy.dispatch_alias_for("macbook-pro")
        assert "task_description" in tool.parameters["properties"]
        assert "委托任务给" in tool.description
        assert "操作应用" in tool.description

    @pytest.mark.asyncio
    async def test_dispatch_alias_for(self) -> None:
        alias = RemoteToolProxy.dispatch_alias_for("macbook-pro")
        assert alias.startswith("mesh_dispatch__")

    @pytest.mark.asyncio
    async def test_no_dispatch_tools_for_hub_nodes(self) -> None:
        """Hub nodes should not get dispatch tools."""
        transport = InMemoryTransport("edge-node", hub_name="hub-only-test")
        await transport.connect()
        registry = MeshRegistry()
        hub = _hub_card()
        await registry.register_node(hub)

        proxy = RemoteToolProxy(
            transport=transport,
            registry=registry,
            local_node_id="edge-node",
        )
        dispatch_tools = proxy.build_dispatch_tools()
        assert len(dispatch_tools) == 0  # Hub should not have dispatch tools


# ---------------------------------------------------------------------------
# TaskRouter: augmented task with agent-loop instructions
# ---------------------------------------------------------------------------


class TestAugmentedTaskAgentLoop:
    @pytest.mark.asyncio
    async def test_augmented_task_includes_edge_nodes(self) -> None:
        """_augment_task 应包含可用边缘节点的描述。"""
        router, _, _ = await _setup_router()
        plan = await router.plan_task(
            session_id="s3",
            task="请在MacBook上打开浏览器登录网站",
            context=[],
        )
        augmented = router._augment_task("请在MacBook上打开浏览器登录网站", plan)
        assert "可用的边缘节点" in augmented
        assert "mesh_dispatch__" in augmented

    @pytest.mark.asyncio
    async def test_prepare_run_injects_dispatch_tools(self) -> None:
        """prepare_run 应注入所有在线 edge 节点的 dispatch 工具。"""
        router, _, _ = await _setup_router()
        routing = await router.prepare_run(
            session_id="s4",
            task="请在MacBook上打开浏览器抓取网页内容",
            context_messages=[],
        )
        dispatch_names = [t.name for t in routing.extra_tools if t.name.startswith("mesh_dispatch__")]
        assert len(dispatch_names) > 0


# ---------------------------------------------------------------------------
# EdgeJournalStore
# ---------------------------------------------------------------------------


class TestEdgeJournalStore:
    def test_ingest_and_list(self, tmp_path: Path) -> None:
        store = EdgeJournalStore(tmp_path / "journal_hub")
        entries = [
            {"entry_id": "e1", "task": "browse", "success": True, "output": "done", "mode": "local"},
            {"entry_id": "e2", "task": "capture", "success": False, "output": "", "mode": "delegated"},
        ]
        accepted = store.ingest(node_id="macbook-pro", entries=entries)
        assert accepted == ["e1", "e2"]
        assert store.entry_count() == 2

    def test_dedup_on_reingest(self, tmp_path: Path) -> None:
        store = EdgeJournalStore(tmp_path / "journal_hub")
        entries = [{"entry_id": "e1", "task": "t1", "success": True}]
        store.ingest(node_id="mac", entries=entries)
        accepted = store.ingest(node_id="mac", entries=entries)
        assert accepted == ["e1"]  # Still accepted for sync ack
        assert store.entry_count() == 1  # No duplicates stored

    def test_list_filter_by_node(self, tmp_path: Path) -> None:
        store = EdgeJournalStore(tmp_path / "journal_hub")
        store.ingest(node_id="mac1", entries=[{"entry_id": "e1", "task": "t1", "success": True}])
        store.ingest(node_id="mac2", entries=[{"entry_id": "e2", "task": "t2", "success": True}])
        assert len(store.list_entries(node_id="mac1")) == 1
        assert len(store.list_entries(node_id="mac2")) == 1
        assert len(store.list_entries()) == 2

    def test_persists_to_disk(self, tmp_path: Path) -> None:
        store_dir = tmp_path / "journal_hub"
        store = EdgeJournalStore(store_dir)
        store.ingest(node_id="mac", entries=[{"entry_id": "e1", "task": "t1", "success": True}])
        files = list((store_dir / "mac").glob("*.json"))
        assert len(files) == 1

        # Reload from disk
        store2 = EdgeJournalStore(store_dir)
        assert store2.entry_count() == 1

    def test_skip_entries_without_id(self, tmp_path: Path) -> None:
        store = EdgeJournalStore(tmp_path / "journal_hub")
        accepted = store.ingest(node_id="mac", entries=[{"task": "no id"}])
        assert accepted == []
        assert store.entry_count() == 0

    def test_entry_gets_synced_at_and_node_id(self, tmp_path: Path) -> None:
        store = EdgeJournalStore(tmp_path / "journal_hub")
        store.ingest(node_id="my-mac", entries=[{"entry_id": "e1", "task": "t"}])
        entries = store.list_entries()
        assert entries[0]["node_id"] == "my-mac"
        assert "synced_at" in entries[0]


# ---------------------------------------------------------------------------
# TaskJournal: sync_to_hub
# ---------------------------------------------------------------------------


class TestTaskJournalSync:
    @pytest.mark.asyncio
    async def test_sync_to_hub_success(self, tmp_path: Path) -> None:
        from nexus.edge.local_runtime import TaskJournal

        journal = TaskJournal(journal_dir=tmp_path / "journal")
        journal.record(
            task="test task", run_id="r1", mode="local", model="m",
            success=True, output="ok", error=None, duration_ms=10, events=[],
        )
        entry_id = journal.unsynced_entries()[0].entry_id
        assert len(journal.unsynced_entries()) == 1

        with patch("nexus.edge.local_runtime.aiohttp") as mock_aiohttp:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.json = AsyncMock(return_value={"entry_ids": [entry_id]})
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)

            mock_session = MagicMock()
            mock_session.post = MagicMock(return_value=mock_response)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
            mock_aiohttp.ClientTimeout = MagicMock()

            synced = await journal.sync_to_hub(
                hub_host="127.0.0.1",
                hub_port=8000,
                node_id="test-mac",
            )
            assert synced == 1
            assert len(journal.unsynced_entries()) == 0

    @pytest.mark.asyncio
    async def test_sync_to_hub_no_entries(self, tmp_path: Path) -> None:
        from nexus.edge.local_runtime import TaskJournal

        journal = TaskJournal(journal_dir=tmp_path / "journal")
        synced = await journal.sync_to_hub(
            hub_host="127.0.0.1",
            hub_port=8000,
            node_id="test-mac",
        )
        assert synced == 0

    @pytest.mark.asyncio
    async def test_sync_to_hub_failure(self, tmp_path: Path) -> None:
        from nexus.edge.local_runtime import TaskJournal

        journal = TaskJournal(journal_dir=tmp_path / "journal")
        journal.record(
            task="test", run_id="r1", mode="local", model="m",
            success=True, output="ok", error=None, duration_ms=10, events=[],
        )

        with patch("nexus.edge.local_runtime.aiohttp.ClientSession") as MockSession:
            MockSession.side_effect = Exception("Connection refused")

            synced = await journal.sync_to_hub(
                hub_host="127.0.0.1",
                hub_port=8000,
                node_id="test-mac",
            )
            assert synced == 0
            assert len(journal.unsynced_entries()) == 1  # Not marked as synced
