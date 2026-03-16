"""Tests for NodeCard data structure."""

from __future__ import annotations

import json

from nexus.mesh.node_card import (
    AvailabilitySpec,
    CapabilitySpec,
    NodeCard,
    NodeType,
    ProviderSpec,
    ResourceSpec,
)


def _make_ubuntu_card() -> NodeCard:
    return NodeCard(
        node_id="ubuntu-server",
        node_type=NodeType.HUB,
        display_name="Ubuntu Server",
        platform="linux",
        arch="x86_64",
        providers=[
            ProviderSpec(name="kimi", model="kimi-k2.5", via="api"),
            ProviderSpec(name="ollama", model="qwen2.5:72b", via="local", context_length=32768),
        ],
        capabilities=[
            CapabilitySpec(
                capability_id="knowledge_store",
                description="Knowledge base storage and retrieval",
                tools=["read_vault", "write_vault", "search_vault"],
            ),
            CapabilitySpec(
                capability_id="local_llm_inference",
                description="Local LLM inference on RTX 5090",
                tools=["local_llm_generate"],
                exclusive=True,
                properties={"privacy": "local_only"},
            ),
            CapabilitySpec(
                capability_id="audio_transcription",
                description="SenseVoice transcription",
                tools=["audio_transcribe_path"],
            ),
        ],
        resources=ResourceSpec(
            cpu_cores=32,
            memory_gb=128,
            gpu="NVIDIA RTX 5090",
            gpu_memory_gb=32,
            disk_free_gb=4000,
        ),
        availability=AvailabilitySpec(schedule="24/7"),
    )


def _make_macbook_card() -> NodeCard:
    return NodeCard(
        node_id="macbook-pro",
        node_type=NodeType.EDGE,
        display_name="MacBook Pro",
        platform="macos",
        arch="arm64",
        providers=[
            ProviderSpec(name="kimi", model="kimi-k2.5", via="api"),
        ],
        capabilities=[
            CapabilitySpec(
                capability_id="browser_automation",
                description="Playwright browser automation",
                tools=["browser_navigate", "browser_extract_text", "browser_screenshot"],
                requires_user_interaction=True,
            ),
            CapabilitySpec(
                capability_id="local_filesystem",
                description="Access macOS local filesystem",
                tools=["list_local_files", "code_read_file"],
            ),
        ],
        resources=ResourceSpec(
            cpu_cores=10,
            memory_gb=16,
            battery_powered=True,
        ),
        availability=AvailabilitySpec(
            intermittent=True,
            max_task_duration_seconds=1800,
        ),
    )


def _make_iphone_card() -> NodeCard:
    return NodeCard(
        node_id="iphone",
        node_type=NodeType.MOBILE,
        display_name="iPhone",
        platform="ios",
        arch="arm64",
        providers=[
            ProviderSpec(name="kimi", model="kimi-k2.5", via="api"),
        ],
        capabilities=[
            CapabilitySpec(
                capability_id="camera_photo",
                description="Take photos",
                tools=["take_photo"],
                requires_user_interaction=True,
            ),
            CapabilitySpec(
                capability_id="push_notification",
                description="Push notifications",
                tools=["push_notify"],
            ),
        ],
        resources=ResourceSpec(
            cpu_cores=6,
            memory_gb=8,
            battery_powered=True,
        ),
        availability=AvailabilitySpec(
            max_task_duration_seconds=300,
            preferred_role="input_output",
        ),
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_node_card_capability_ids():
    card = _make_ubuntu_card()
    ids = card.capability_ids()
    assert ids == {"knowledge_store", "local_llm_inference", "audio_transcription"}


def test_node_card_tool_names():
    card = _make_ubuntu_card()
    tools = card.tool_names()
    assert "read_vault" in tools
    assert "local_llm_generate" in tools
    assert "audio_transcribe_path" in tools


def test_node_card_find_capability():
    card = _make_ubuntu_card()
    cap = card.find_capability("local_llm_inference")
    assert cap is not None
    assert cap.exclusive is True
    assert cap.properties.get("privacy") == "local_only"

    assert card.find_capability("nonexistent") is None


def test_node_card_has_local_llm():
    ubuntu = _make_ubuntu_card()
    macbook = _make_macbook_card()
    assert ubuntu.has_local_llm() is True
    assert macbook.has_local_llm() is False


def test_node_card_roundtrip_json():
    original = _make_ubuntu_card()
    json_str = original.to_json()
    restored = NodeCard.from_json(json_str)

    assert restored.node_id == original.node_id
    assert restored.node_type == original.node_type
    assert restored.display_name == original.display_name
    assert restored.platform == original.platform
    assert restored.arch == original.arch
    assert restored.capability_ids() == original.capability_ids()
    assert restored.tool_names() == original.tool_names()
    assert restored.has_local_llm() == original.has_local_llm()
    assert restored.resources.gpu == "NVIDIA RTX 5090"
    assert restored.resources.gpu_memory_gb == 32
    assert restored.availability.schedule == "24/7"
    assert restored.version == original.version


def test_node_card_roundtrip_dict():
    original = _make_macbook_card()
    d = original.to_dict()
    restored = NodeCard.from_dict(d)

    assert restored.node_id == "macbook-pro"
    assert restored.node_type == NodeType.EDGE
    assert restored.resources.battery_powered is True
    assert restored.availability.intermittent is True
    assert restored.availability.max_task_duration_seconds == 1800
    caps = restored.capabilities
    browser_cap = next(c for c in caps if c.capability_id == "browser_automation")
    assert browser_cap.requires_user_interaction is True
    assert "browser_navigate" in browser_cap.tools


def test_node_card_from_yaml():
    yaml_text = """
node_id: test-node
node_type: edge
display_name: Test Node
platform: linux
capabilities:
  - capability_id: test_cap
    description: A test capability
    tools:
      - tool_a
      - tool_b
resources:
  cpu_cores: 4
  memory_gb: 8
"""
    card = NodeCard.from_yaml(yaml_text)
    assert card.node_id == "test-node"
    assert card.node_type == NodeType.EDGE
    assert card.capability_ids() == {"test_cap"}
    assert card.resources.cpu_cores == 4


def test_node_card_mobile():
    card = _make_iphone_card()
    assert card.node_type == NodeType.MOBILE
    assert card.availability.preferred_role == "input_output"
    assert card.availability.max_task_duration_seconds == 300
    assert card.resources.battery_powered is True


def test_node_card_version():
    card = _make_ubuntu_card()
    assert card.version == 1
    card.version = 2
    d = card.to_dict()
    assert d["version"] == 2
    restored = NodeCard.from_dict(d)
    assert restored.version == 2
