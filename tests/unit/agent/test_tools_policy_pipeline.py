from __future__ import annotations

import asyncio

from nexus.agent.tools_policy import PolicyLayer, ToolPolicyPipeline, ToolsPolicy
from nexus.agent.types import ToolCall, ToolDefinition, ToolRiskLevel


def _make_tool(name: str, risk: ToolRiskLevel = ToolRiskLevel.LOW) -> ToolDefinition:
    async def _noop(**kwargs):
        return kwargs

    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=_noop,
        risk_level=risk,
    )


def test_pipeline_filters_tools_with_and_semantics():
    tools = [
        _make_tool("read_vault"),
        _make_tool("search_web"),
        _make_tool("system_run", ToolRiskLevel.HIGH),
    ]
    pipeline = ToolPolicyPipeline(
        [
            PolicyLayer(name="profile", allow=["read_*", "search_*", "system_*"]),
            PolicyLayer(name="model", deny=["search_*"]),
            PolicyLayer(name="risk", max_risk_level=ToolRiskLevel.MEDIUM),
        ]
    )

    filtered = pipeline.filter_tools(tools)

    assert [tool.name for tool in filtered] == ["read_vault"]


def test_pipeline_supports_glob_matching():
    tools = [
        _make_tool("browser_navigate"),
        _make_tool("browser_extract_text"),
        _make_tool("system_run"),
    ]
    pipeline = ToolPolicyPipeline([PolicyLayer(name="channel", allow=["browser_*"])])

    filtered = pipeline.filter_tools(tools)

    assert {tool.name for tool in filtered} == {"browser_navigate", "browser_extract_text"}


def test_pipeline_also_allow_restores_tool_filtered_upstream():
    tools = [
        _make_tool("read_vault"),
        _make_tool("dispatch_subagent"),
    ]
    pipeline = ToolPolicyPipeline(
        [
            PolicyLayer(name="profile", allow=["read_*"]),
            PolicyLayer(name="channel", also_allow=["dispatch_*"]),
        ]
    )

    filtered = pipeline.filter_tools(tools)

    assert [tool.name for tool in filtered] == ["read_vault", "dispatch_subagent"]


def test_pipeline_limits_tool_count_after_filtering():
    tools = [
        _make_tool("a_tool"),
        _make_tool("b_tool"),
        _make_tool("c_tool"),
    ]
    pipeline = ToolPolicyPipeline(
        [PolicyLayer(name="model", allow=["*_tool"], max_tools_count=2)]
    )

    filtered = pipeline.filter_tools(tools)

    assert [tool.name for tool in filtered] == ["a_tool", "b_tool"]


def test_tools_policy_keeps_legacy_risk_gating():
    policy = ToolsPolicy()
    tool = _make_tool("dangerous", ToolRiskLevel.HIGH)
    call = ToolCall(call_id="c-1", tool_name="dangerous", arguments={})

    result = asyncio.run(policy.check(call, tool))

    assert result.allowed is False
    assert result.requires_approval is True


def test_tools_policy_allows_mesh_tool_to_defer_target_approval():
    policy = ToolsPolicy()
    tool = ToolDefinition(
        name="mesh__abc__run",
        description="mesh tool",
        parameters={"type": "object", "properties": {}},
        handler=lambda **kwargs: kwargs,  # pragma: no cover - not executed
        risk_level=ToolRiskLevel.CRITICAL,
        requires_approval=True,
        tags=["mesh"],
    )
    call = ToolCall(call_id="c-2", tool_name=tool.name, arguments={})

    result = asyncio.run(policy.check(call, tool))

    assert result.allowed is True
    assert result.requires_approval is False
