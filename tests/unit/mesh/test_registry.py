"""Tests for MeshRegistry transport integration."""

from __future__ import annotations

import time

import pytest

from nexus.mesh import (
    AvailabilitySpec,
    CapabilitySpec,
    InMemoryTransport,
    MeshRegistry,
    MessageType,
    NodeCard,
    NodeType,
    ProviderSpec,
    ResourceSpec,
)


def _make_hub_card() -> NodeCard:
    return NodeCard(
        node_id="ubuntu-server-5090",
        node_type=NodeType.HUB,
        display_name="Ubuntu Server",
        platform="linux",
        arch="x86_64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=[
            CapabilitySpec(
                capability_id="knowledge_store",
                description="Knowledge base storage",
                tools=["read_vault", "write_vault", "search_vault"],
            )
        ],
        resources=ResourceSpec(cpu_cores=32, memory_gb=128, battery_powered=False),
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
                description="Playwright browser automation",
                tools=["browser_navigate", "browser_extract_text"],
                requires_user_interaction=True,
            )
        ],
        resources=ResourceSpec(cpu_cores=10, memory_gb=16, battery_powered=True),
        availability=AvailabilitySpec(intermittent=True, max_task_duration_seconds=1800),
    )


@pytest.mark.asyncio
async def test_registry_discovers_remote_node_via_transport():
    InMemoryTransport.reset_hub("mesh-registry")
    hub_transport = InMemoryTransport("ubuntu-server-5090", hub_name="mesh-registry")
    edge_transport = InMemoryTransport("macbook-pro", hub_name="mesh-registry")
    await hub_transport.connect()
    await edge_transport.connect()

    registry = MeshRegistry(hub_transport)
    await registry.setup_transport_handlers()
    await registry.register_node(_make_hub_card())

    edge_card = _make_edge_card()
    await edge_transport.publish(
        f"nexus/nodes/{edge_card.node_id}/card",
        edge_transport.make_message(
            MessageType.NODE_REGISTER,
            f"nexus/nodes/{edge_card.node_id}/card",
            edge_card.to_dict(),
        ),
    )
    await edge_transport.publish(
        f"nexus/nodes/{edge_card.node_id}/heartbeat",
        edge_transport.make_message(
            MessageType.NODE_HEARTBEAT,
            f"nexus/nodes/{edge_card.node_id}/heartbeat",
            {"current_load": 0.25, "active_tasks": 2, "battery_level": 82},
        ),
    )

    discovered = registry.get_node("macbook-pro")
    assert discovered is not None
    assert discovered.display_name == "MacBook Pro"
    assert set(registry.list_online_node_ids()) == {"ubuntu-server-5090", "macbook-pro"}

    browser_nodes = registry.query_capability("browser_automation")
    assert [entry.node_id for entry in browser_nodes] == ["macbook-pro"]

    status = registry.get_node_status("macbook-pro")
    assert status is not None
    assert status.current_load == pytest.approx(0.25)
    assert status.active_tasks == 2
    assert status.battery_level == 82


@pytest.mark.asyncio
async def test_registry_marks_node_offline_after_timeout():
    registry = MeshRegistry(heartbeat_timeout_seconds=10)
    await registry.register_node(_make_edge_card())

    status = registry.get_node_status("macbook-pro")
    assert status is not None
    status.last_heartbeat = time.time() - 60

    timed_out = await registry.check_timeouts()

    assert timed_out == ["macbook-pro"]
    assert registry.get_node_status("macbook-pro").online is False


@pytest.mark.asyncio
async def test_registry_does_not_timeout_hub_node():
    registry = MeshRegistry(heartbeat_timeout_seconds=10)
    await registry.register_node(_make_hub_card())

    status = registry.get_node_status("ubuntu-server-5090")
    assert status is not None
    status.last_heartbeat = time.time() - 60

    timed_out = await registry.check_timeouts()

    assert timed_out == []
    assert registry.get_node_status("ubuntu-server-5090").online is True
