from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nexus.agent.attempt import AttemptBuilder
from nexus.agent.run import RunManager
from nexus.agent.run_store import RunStore
from nexus.agent.types import ToolDefinition, ToolRiskLevel
from nexus.agent.tools_policy import ToolsPolicy
from nexus.evolution.audit import AuditLog
from nexus.evolution.sandbox import Sandbox
from nexus.evolution.skill_manager import SkillManager


class _FakeGateway:
    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        payload = self._responses.pop(0) if self._responses else "done"
        if isinstance(payload, dict):
            return payload
        return {
            "message": {
                "role": "assistant",
                "content": payload,
                "tool_calls": [],
            }
        }


class _FakeChangeResult:
    def __init__(self, success: bool, reason: str):
        self.success = success
        self.reason = reason


class _FakeCapabilityManager:
    def __init__(self, capabilities: list[dict[str, Any]]):
        self._items = {str(item["capability_id"]): dict(item) for item in capabilities}
        self.enable_calls: list[str] = []

    def list_capabilities(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items.values()]

    def get_status(self, capability_id: str) -> dict[str, Any]:
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


def _create_installable_bundle(registry_dir: Path, skill_id: str = "office-conversion") -> None:
    skill_dir = registry_dir / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: Office Conversion\n"
            "description: 处理 PPT/PPTX 文档转换。\n"
            "tags:\n"
            "  - office\n"
            "  - ppt\n"
            "  - pdf\n"
            "keywords:\n"
            "  - ppt\n"
            "  - pdf\n"
            "  - 转换\n"
            "  - 演示文稿\n"
            "---\n\n"
            "# Office Conversion\n\n"
            "遇到 PPT 转 PDF 时，优先完成任务，不要先说做不到。\n"
        ),
        encoding="utf-8",
    )


def _create_installed_skill(skills_dir: Path, skill_id: str, *, name: str, description: str, body: str, tags: str = "") -> None:
    skill_dir = skills_dir / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if tags:
        frontmatter.append(f"tags: {tags}")
    frontmatter.append("---")
    (skill_dir / "SKILL.md").write_text(
        "\n".join(frontmatter) + f"\n\n{body}\n",
        encoding="utf-8",
    )


def _build_manager(
    tmp_path: Path,
    gateway: _FakeGateway,
    *,
    capability_manager: _FakeCapabilityManager | None = None,
    available_tools: list[ToolDefinition] | None = None,
) -> tuple[RunManager, SkillManager]:
    skills_dir = tmp_path / "skills"
    registry_dir = tmp_path / "skill_registry"
    _create_installable_bundle(registry_dir)
    audit = AuditLog(tmp_path / "audit.db")
    sandbox = Sandbox(tmp_path / "staging")
    skill_manager = SkillManager(
        skills_dir,
        sandbox,
        audit,
        catalog_dir=registry_dir,
        system_runner=None,
        python_executable=None,
    )
    attempt_builder = AttemptBuilder(
        available_tools=available_tools or [],
        skill_manager=skill_manager,
    )
    run_manager = RunManager(
        run_store=RunStore(tmp_path / "runs.db"),
        attempt_builder=attempt_builder,
        provider=gateway,
        tools_policy=ToolsPolicy(),
        fallback_models=["qwen-test"],
        capability_manager=capability_manager,
        skill_manager=skill_manager,
    )
    return run_manager, skill_manager


def test_run_manager_auto_installs_and_preloads_matching_skill(tmp_path):
    gateway = _FakeGateway(["done"])
    run_manager, skill_manager = _build_manager(tmp_path, gateway)

    run = asyncio.run(
        run_manager.execute(
            session_id="s-1",
            task="请把PPT转换为PDF",
            context_messages=[{"role": "user", "content": "请把PPT转换为PDF"}],
            model="qwen-test",
        )
    )

    assert run.result == "done"
    assert (tmp_path / "skills" / "office-conversion").is_dir()
    assert "office-conversion" in run.metadata.get("auto_installed_skills", [])
    assert "office-conversion" in run.metadata.get("auto_preloaded_skills", [])
    assert "Office Conversion" in gateway.calls[0]["messages"][0]["content"]

    installed = {item["skill_id"] for item in skill_manager.list_skills()}
    assert "office-conversion" in installed


def test_run_manager_retries_after_missing_skill_response(tmp_path):
    gateway = _FakeGateway(
        [
            "目前没有演示文稿转换能力。",
            "现在可以处理了。",
        ]
    )
    run_manager, _skill_manager = _build_manager(tmp_path, gateway)

    run = asyncio.run(
        run_manager.execute(
            session_id="s-2",
            task="演示文稿输出",
            context_messages=[{"role": "user", "content": "演示文稿输出"}],
            model="qwen-test",
        )
    )

    assert run.result == "现在可以处理了。"
    assert run.attempt_count == 2
    assert "office-conversion" in run.metadata.get("auto_preloaded_skills", [])


def test_run_manager_auto_preloads_matching_installed_skill(tmp_path):
    gateway = _FakeGateway(["done"])
    run_manager, skill_manager = _build_manager(tmp_path, gateway)
    _create_installed_skill(
        tmp_path / "skills",
        "excel-processing",
        name="Excel Processing",
        description="处理 Excel 读取和 CSV 转换。",
        body="# Excel Processing\n遇到 Excel/CSV 任务时，优先使用 excel_list_sheets 和 excel_to_csv。",
        tags="excel,csv,转换",
    )

    run = asyncio.run(
        run_manager.execute(
            session_id="s-3",
            task="请把这个Excel转成CSV",
            context_messages=[{"role": "user", "content": "请把这个Excel转成CSV"}],
            model="qwen-test",
        )
    )

    assert run.result == "done"
    assert "excel-processing" in run.metadata.get("auto_preloaded_skills", [])
    assert "excel_list_sheets" in gateway.calls[0]["messages"][0]["content"]
    assert "Excel Processing" in skill_manager.get_skill_descriptions()


def test_run_manager_auto_enables_matching_capability_and_preloads_skill_hint(tmp_path):
    gateway = _FakeGateway(["done"])
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
    run_manager, _skill_manager = _build_manager(tmp_path, gateway, capability_manager=capability_manager)
    _create_installed_skill(
        tmp_path / "skills",
        "excel-processing",
        name="Excel Processing",
        description="处理 Excel 读取和 CSV 转换。",
        body="# Excel Processing\n遇到 Excel/CSV 任务时，优先使用 excel_list_sheets 和 excel_to_csv。",
        tags="excel,csv,转换",
    )

    run = asyncio.run(
        run_manager.execute(
            session_id="s-4",
            task="把Excel工作簿转换成CSV",
            context_messages=[{"role": "user", "content": "把Excel工作簿转换成CSV"}],
            model="qwen-test",
        )
    )

    assert run.result == "done"
    assert capability_manager.enable_calls == ["excel_processing"]
    assert "excel_processing" in run.metadata.get("auto_enabled_capabilities", [])
    assert "excel-processing" in run.metadata.get("auto_preloaded_skills", [])


def test_run_manager_retries_after_missing_capability_response(tmp_path):
    gateway = _FakeGateway(
        [
            "Excel capability is not enabled. Run capability_enable('excel_processing') first.",
            "现在已经可以处理 Excel 了。",
        ]
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
    run_manager, _skill_manager = _build_manager(tmp_path, gateway, capability_manager=capability_manager)
    _create_installed_skill(
        tmp_path / "skills",
        "excel-processing",
        name="Excel Processing",
        description="处理 Excel 读取和 CSV 转换。",
        body="# Excel Processing\n遇到 Excel/CSV 任务时，优先使用 excel_list_sheets 和 excel_to_csv。",
        tags="excel,csv,转换",
    )

    run = asyncio.run(
        run_manager.execute(
            session_id="s-5",
            task="处理 Excel 文件",
            context_messages=[{"role": "user", "content": "处理 Excel 文件"}],
            model="qwen-test",
        )
    )

    assert run.result == "现在已经可以处理 Excel 了。"
    assert run.attempt_count == 2
    assert "excel_processing" in run.metadata.get("auto_enabled_capabilities", [])
    assert "excel-processing" in run.metadata.get("auto_preloaded_skills", [])


def test_run_manager_records_successful_mesh_dispatches(tmp_path):
    async def dispatch_handler(task_description: str, constraints: str = "") -> str:
        return f"任务已异步派发：{task_description} {constraints}".strip()

    dispatch_tool = ToolDefinition(
        name="mesh_dispatch__bWFjYm9vay1wcm8",
        description="Dispatch to MacBook Pro",
        parameters={
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "constraints": {"type": "string"},
            },
            "required": ["task_description"],
        },
        handler=dispatch_handler,
        risk_level=ToolRiskLevel.MEDIUM,
        tags=["mesh", "dispatch", "node:macbook-pro"],
    )
    gateway = _FakeGateway([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-mesh-1",
                        "type": "function",
                        "function": {
                            "name": dispatch_tool.name,
                            "arguments": {
                                "task_description": "打开语音备忘录，并开始录制",
                            },
                        },
                    }
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "已完成派发",
                "tool_calls": [],
            }
        },
    ])
    run_manager, _skill_manager = _build_manager(
        tmp_path,
        gateway,
        available_tools=[dispatch_tool],
    )

    run = asyncio.run(
        run_manager.execute(
            session_id="s-dispatch",
            task="打开语音备忘录，并开始录制",
            context_messages=[{"role": "user", "content": "打开语音备忘录，并开始录制"}],
            model="qwen-test",
        )
    )

    assert run.result == "已完成派发"
    assert run.metadata.get("successful_mesh_dispatches") == [
        {
            "tool": dispatch_tool.name,
            "task_description": "打开语音备忘录，并开始录制",
        }
    ]
