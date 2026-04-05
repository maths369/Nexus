from __future__ import annotations

import asyncio
from pathlib import Path

from nexus.agent.attempt import AttemptBuilder
from nexus.agent.run import RunManager
from nexus.agent.tools_policy import ToolsPolicy
from nexus.agent.tool_profiles import ToolProfile
from nexus.agent.types import ToolDefinition, ToolRiskLevel
from nexus.agent.run_store import RunStore
from nexus.shared import load_nexus_settings


class _RecordingGateway:
    def __init__(self):
        self.calls: list[dict] = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "message": {
                "role": "assistant",
                "content": "done",
                "tool_calls": [],
            }
        }


def _tool(name: str, risk: ToolRiskLevel = ToolRiskLevel.LOW) -> ToolDefinition:
    async def _noop(**kwargs):
        return kwargs

    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=_noop,
        risk_level=risk,
    )


def _write_settings(root: Path, body: str):
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "app.yaml").write_text(body, encoding="utf-8")
    return load_nexus_settings(root_dir=root)


def test_run_manager_applies_model_policy_to_tool_injection(tmp_path):
    settings = _write_settings(
        tmp_path,
        "\n".join(
            [
                "model_policies:",
                "  qwen-mini:",
                "    deny:",
                "    - system_run",
                "    max_tools_count: 2",
            ]
        ),
    )
    gateway = _RecordingGateway()
    attempt_builder = AttemptBuilder(
        available_tools=[
            _tool("read_vault"),
            _tool("search_web"),
            _tool("system_run", ToolRiskLevel.MEDIUM),
        ]
    )
    manager = RunManager(
        run_store=RunStore(tmp_path / "runs.db"),
        attempt_builder=attempt_builder,
        provider=gateway,
        tools_policy=ToolsPolicy(),
        fallback_models=["qwen-mini"],
        settings=settings,
    )

    run = asyncio.run(
        manager.execute(
            session_id="s-1",
            task="读文件并搜索",
            context_messages=[{"role": "user", "content": "读文件并搜索"}],
            model="qwen-mini",
        )
    )

    assert run.result == "done"
    tool_names = [item["function"]["name"] for item in gateway.calls[0]["tools"]]
    assert tool_names == ["read_vault", "search_web"]
    assert "model:qwen-mini" in run.metadata["tool_policy_layers"]


def test_run_manager_applies_channel_policy_to_tool_injection(tmp_path):
    settings = _write_settings(
        tmp_path,
        "\n".join(
            [
                "channel_policies:",
                "  feishu:",
                "    deny:",
                "    - system_run",
                "    groups:",
                "      default:",
                "        also_allow:",
                "        - dispatch_subagent",
            ]
        ),
    )
    gateway = _RecordingGateway()
    attempt_builder = AttemptBuilder(
        available_tools=[
            _tool("read_vault"),
            _tool("system_run", ToolRiskLevel.MEDIUM),
            _tool("dispatch_subagent", ToolRiskLevel.MEDIUM),
        ]
    )
    manager = RunManager(
        run_store=RunStore(tmp_path / "runs.db"),
        attempt_builder=attempt_builder,
        provider=gateway,
        tools_policy=ToolsPolicy(),
        fallback_models=["qwen-plus"],
        settings=settings,
    )

    run = asyncio.run(
        manager.execute(
            session_id="s-2",
            task="请检查这个文件并委派子代理",
            context_messages=[{"role": "user", "content": "请检查这个文件并委派子代理"}],
            model="qwen-plus",
            tool_profile=ToolProfile.coding(),
            channel="feishu",
            group_id="chat-anything",
        )
    )

    assert run.result == "done"
    tool_names = [item["function"]["name"] for item in gateway.calls[0]["tools"]]
    assert "system_run" not in tool_names
    assert "dispatch_subagent" in tool_names
    assert "channel:feishu" in run.metadata["tool_policy_layers"]
