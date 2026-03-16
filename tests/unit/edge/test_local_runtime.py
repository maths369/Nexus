"""Tests for EdgeAgentRuntime, TaskJournal, and dual-mode execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.agent.types import RunEvent, ToolDefinition, ToolResult, ToolRiskLevel
from nexus.edge.local_runtime import (
    DELEGATED_SYSTEM_PROMPT,
    EDGE_SYSTEM_PROMPT,
    EdgeAgentRuntime,
    JournalEntry,
    LocalRunResult,
    TaskJournal,
    build_edge_provider,
)
from nexus.mesh.node_card import NodeCard, ProviderSpec
from nexus.edge.macos_sidecar import _provider_configs_from_node_card


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _dummy_tool(name: str = "test_tool") -> ToolDefinition:
    async def handler(**kwargs: object) -> str:
        return f"executed {name}"

    return ToolDefinition(
        name=name,
        description=f"Test tool {name}",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        risk_level=ToolRiskLevel.LOW,
    )


def _mock_provider_gateway(model: str = "test-model") -> MagicMock:
    provider_info = MagicMock()
    provider_info.model = model
    provider_info.name = "test-provider"
    gateway = MagicMock()
    gateway.get_provider.return_value = provider_info
    return gateway


# ---------------------------------------------------------------------------
# TaskJournal
# ---------------------------------------------------------------------------


class TestTaskJournal:
    def test_record_and_unsynced(self) -> None:
        journal = TaskJournal()
        entry = journal.record(
            task="do something",
            run_id="run-1",
            mode="local",
            model="test-model",
            success=True,
            output="done",
            error=None,
            duration_ms=100.0,
            events=[],
        )
        assert entry.task == "do something"
        assert entry.synced is False
        assert len(journal.unsynced_entries()) == 1

    def test_mark_synced(self) -> None:
        journal = TaskJournal()
        e1 = journal.record(
            task="t1", run_id="r1", mode="local", model="m",
            success=True, output="ok", error=None, duration_ms=10, events=[],
        )
        e2 = journal.record(
            task="t2", run_id="r2", mode="local", model="m",
            success=True, output="ok", error=None, duration_ms=10, events=[],
        )
        journal.mark_synced([e1.entry_id])
        unsynced = journal.unsynced_entries()
        assert len(unsynced) == 1
        assert unsynced[0].entry_id == e2.entry_id

    def test_persist_to_disk(self, tmp_path: Path) -> None:
        journal = TaskJournal(journal_dir=tmp_path / "journal")
        journal.record(
            task="persist test", run_id="r1", mode="local", model="m",
            success=True, output="ok", error=None, duration_ms=5, events=[],
        )
        files = list((tmp_path / "journal").glob("*.json"))
        assert len(files) == 1

    def test_record_extracts_tool_calls(self) -> None:
        events = [
            RunEvent(event_id="e1", run_id="r1", event_type="tool_result", data={"tool": "browser_navigate", "success": True}),
            RunEvent(event_id="e2", run_id="r1", event_type="llm_response", data={"content": "thinking..."}),
            RunEvent(event_id="e3", run_id="r1", event_type="tool_result", data={"tool": "capture_screen", "success": False}),
        ]
        journal = TaskJournal()
        entry = journal.record(
            task="multi-step", run_id="r1", mode="local", model="m",
            success=True, output="done", error=None, duration_ms=200, events=events,
        )
        assert len(entry.tool_calls) == 2
        assert entry.tool_calls[0]["tool"] == "browser_navigate"
        assert entry.tool_calls[1]["success"] is False


class TestJournalEntry:
    def test_to_dict_truncates_output(self) -> None:
        entry = JournalEntry(
            entry_id="abc",
            timestamp=1000.0,
            task="test",
            run_id="r1",
            mode="local",
            model="m",
            success=True,
            output="x" * 1000,
            error=None,
            duration_ms=10.0,
            tool_calls=[],
        )
        d = entry.to_dict()
        assert len(d["output"]) == 500


# ---------------------------------------------------------------------------
# EdgeAgentRuntime
# ---------------------------------------------------------------------------


class TestEdgeAgentRuntime:
    @pytest.mark.asyncio
    async def test_run_local_success(self) -> None:
        gateway = _mock_provider_gateway()
        tools = [_dummy_tool("tool_a")]
        runtime = EdgeAgentRuntime(provider=gateway, tools=tools)

        mock_output = "Task completed successfully"
        mock_events = [RunEvent(event_id="e1", run_id="r1", event_type="llm_response", data={"content": mock_output})]

        with patch("nexus.edge.local_runtime.execute_tool_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (mock_output, mock_events)
            result = await runtime.run_local("do something useful")

        assert result.success is True
        assert result.output == mock_output
        assert result.run_id.startswith("edge-")
        assert result.model == "test-model"
        assert result.duration_ms > 0
        # Journal should have recorded it
        assert len(runtime.journal.unsynced_entries()) == 1

    @pytest.mark.asyncio
    async def test_run_local_failure(self) -> None:
        gateway = _mock_provider_gateway()
        runtime = EdgeAgentRuntime(provider=gateway, tools=[])

        with patch("nexus.edge.local_runtime.execute_tool_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.side_effect = RuntimeError("LLM unreachable")
            result = await runtime.run_local("broken task")

        assert result.success is False
        assert result.error == "LLM unreachable"
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_run_delegated_success(self) -> None:
        gateway = _mock_provider_gateway()
        tools = [_dummy_tool("tool_a"), _dummy_tool("tool_b")]
        runtime = EdgeAgentRuntime(provider=gateway, tools=tools)

        with patch("nexus.edge.local_runtime.execute_tool_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = ("delegated done", [])
            result = await runtime.run_delegated("Hub says: extract data from browser")

        assert result.success is True
        assert result.run_id.startswith("delegated-")
        journal_entries = runtime.journal.unsynced_entries()
        assert len(journal_entries) == 1
        assert journal_entries[0].mode == "delegated"

    @pytest.mark.asyncio
    async def test_run_local_with_extra_tools(self) -> None:
        gateway = _mock_provider_gateway()
        base_tools = [_dummy_tool("tool_a")]
        extra = [_dummy_tool("tool_b"), _dummy_tool("tool_a")]  # duplicate should be skipped
        runtime = EdgeAgentRuntime(provider=gateway, tools=base_tools)

        with patch("nexus.edge.local_runtime.execute_tool_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = ("ok", [])
            await runtime.run_local("task", extra_tools=extra)

        call_args = mock_loop.call_args
        config = call_args.kwargs["config"]
        tool_names = [t.name for t in config.tools]
        assert tool_names == ["tool_a", "tool_b"]  # no duplicate

    @pytest.mark.asyncio
    async def test_run_local_uses_custom_system_prompt(self) -> None:
        gateway = _mock_provider_gateway()
        runtime = EdgeAgentRuntime(provider=gateway, tools=[])

        with patch("nexus.edge.local_runtime.execute_tool_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = ("ok", [])
            await runtime.run_local("task", system_prompt="Custom prompt")

        config = mock_loop.call_args.kwargs["config"]
        assert config.system_prompt == "Custom prompt"

    def test_set_tools(self) -> None:
        gateway = _mock_provider_gateway()
        runtime = EdgeAgentRuntime(provider=gateway, tools=[_dummy_tool("a")])
        runtime.set_tools([_dummy_tool("b"), _dummy_tool("c")])
        assert len(runtime._tools) == 2


# ---------------------------------------------------------------------------
# build_edge_provider
# ---------------------------------------------------------------------------


class TestBuildEdgeProvider:
    def test_returns_none_for_empty_configs(self) -> None:
        assert build_edge_provider([]) is None

    def test_returns_none_when_no_api_key(self) -> None:
        configs = [{"name": "test", "model": "test-model"}]
        assert build_edge_provider(configs) is None

    def test_builds_gateway_with_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
        configs = [
            {
                "name": "test",
                "model": "test-model",
                "base_url": "https://api.example.com/v1",
                "api_key_env": "TEST_API_KEY",
            }
        ]
        gateway = build_edge_provider(configs)
        assert gateway is not None


# ---------------------------------------------------------------------------
# _provider_configs_from_node_card
# ---------------------------------------------------------------------------


class TestProviderConfigsFromNodeCard:
    def test_extracts_api_providers(self) -> None:
        card = NodeCard(
            node_id="test-mac",
            node_type="edge",
            display_name="Test Mac",
            platform="macos",
            providers=[
                ProviderSpec(
                    name="kimi",
                    model="kimi-k2.5",
                    via="api",
                    properties={
                        "provider_type": "moonshot",
                        "base_url": "https://api.moonshot.cn/v1",
                        "api_key_env": "MOONSHOT_API_KEY",
                    },
                ),
                ProviderSpec(name="ollama", model="qwen2.5:72b", via="local"),
            ],
        )
        configs = _provider_configs_from_node_card(card)
        assert len(configs) == 1
        assert configs[0]["name"] == "kimi"
        assert configs[0]["base_url"] == "https://api.moonshot.cn/v1"
        assert configs[0]["api_key_env"] == "MOONSHOT_API_KEY"

    def test_skips_local_providers(self) -> None:
        card = NodeCard(
            node_id="test",
            node_type="hub",
            display_name="Test",
            platform="linux",
            providers=[ProviderSpec(name="ollama", model="qwen2.5:72b", via="local")],
        )
        assert _provider_configs_from_node_card(card) == []


# ---------------------------------------------------------------------------
# LocalRunResult
# ---------------------------------------------------------------------------


class TestLocalRunResult:
    def test_to_dict(self) -> None:
        result = LocalRunResult(
            run_id="edge-abc",
            task="test task",
            success=True,
            output="done",
            events=[RunEvent(event_id="e1", run_id="r1", event_type="llm_response", data={})],
            duration_ms=150.0,
            model="kimi-k2.5",
        )
        d = result.to_dict()
        assert d["run_id"] == "edge-abc"
        assert d["event_count"] == 1
        assert d["model"] == "kimi-k2.5"
