"""Runtime-level smoke checks for core agent capabilities."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.agent.types import Run
from nexus.agent.tool_profiles import ToolProfile
from nexus.api.runtime import NexusRuntime


@dataclass
class SmokeCheck:
    name: str
    ok: bool
    detail: str


class _SmokeProvider:
    async def chat_completion(self, **kwargs) -> dict[str, Any]:
        return {
            "message": {
                "role": "assistant",
                "content": "subagent smoke ok",
                "tool_calls": [],
            }
        }


def _tool_map(runtime: NexusRuntime) -> dict[str, Any]:
    return {tool.name: tool for tool in runtime.available_tools}


async def run_agent_capability_smoke(runtime: NexusRuntime) -> list[SmokeCheck]:
    checks: list[SmokeCheck] = []
    tool_map = _tool_map(runtime)
    expected_tools = {
        "compact",
        "load_skill",
        "skill_list_installable",
        "skill_install",
        "skill_search_remote",
        "skill_import_local",
        "skill_import_remote",
        "skill_create",
        "skill_update",
        "skill_list_installed",
        "evolution_audit",
        "capability_list_available",
        "capability_status",
        "capability_enable",
        "capability_create",
        "capability_register",
        "capability_stage",
        "capability_verify",
        "capability_promote",
        "capability_rollback",
        "excel_list_sheets",
        "excel_to_csv",
        "todo_write",
        "dispatch_subagent",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "background_run",
        "check_background",
        "system_run",
        "read_local_file",
        "code_read_file",
        "write_local_file",
        "file_write",
        "file_edit",
        "file_search",
    }
    missing_tools = sorted(expected_tools - set(tool_map))
    checks.append(
        SmokeCheck(
            name="runtime_tools",
            ok=not missing_tools,
            detail="all expected tools present" if not missing_tools else f"missing: {', '.join(missing_tools)}",
        )
    )

    wiring_ok = all(
        (
            runtime.attempt_builder._skill_manager is runtime.skill_manager,  # noqa: SLF001
            runtime.run_manager._compressor is runtime.compressor,  # noqa: SLF001
            runtime.run_manager._todo_manager is runtime.todo_manager,  # noqa: SLF001
            runtime.run_manager._background_manager is runtime.background_manager,  # noqa: SLF001
            runtime.run_manager._capability_promotion_advisor is runtime.capability_promotion_advisor,  # noqa: SLF001
        )
    )
    checks.append(
        SmokeCheck(
            name="runtime_wiring",
            ok=wiring_ok,
            detail="attempt/run manager wiring is active" if wiring_ok else "manager injection mismatch",
        )
    )

    smoke_skill_path = runtime.paths.skills / "smoke-skill"
    smoke_transcript_dir = runtime.paths.staging / "smoke_transcripts"
    smoke_skill_path.mkdir(parents=True, exist_ok=True)
    try:
        (smoke_skill_path / "SKILL.md").write_text(
            "---\nname: smoke-skill\ndescription: smoke capability\n---\n## smoke\nUse this skill for smoke verification.\n",
            encoding="utf-8",
        )

        attempt = await runtime.attempt_builder.build(
            run=Run(run_id="smoke-run", session_id="smoke-session", task="请验证 smoke skill 注入", model="smoke-model"),
            context_messages=[],
            model="smoke-model",
        )
        skill_prompt_ok = "smoke-skill" in attempt.system_prompt and "load_skill" in attempt.system_prompt
        checks.append(
            SmokeCheck(
                name="skill_layer1",
                ok=skill_prompt_ok,
                detail="skill descriptions injected into system prompt" if skill_prompt_ok else "skill descriptions missing",
            )
        )

        load_skill_result = await tool_map["load_skill"].handler("smoke-skill")
        load_skill_ok = (
            "Use this skill for smoke verification." in load_skill_result
            and "<location>" in load_skill_result
            and "<root>" in load_skill_result
        )
        checks.append(
            SmokeCheck(
                name="skill_layer2",
                ok=load_skill_ok,
                detail="load_skill returned full skill content with paths" if load_skill_ok else "load_skill content mismatch",
            )
        )

        listed_skills = await tool_map["skill_list_installed"].handler()
        evolution_inventory_ok = "smoke-skill" in listed_skills or "meeting-transcription" in listed_skills
        checks.append(
            SmokeCheck(
                name="evolution_tools",
                ok=evolution_inventory_ok,
                detail=listed_skills,
            )
        )

        installable_skills = await tool_map["skill_list_installable"].handler(query="ppt pdf 转换")
        installable_ok = "office-conversion" in installable_skills
        checks.append(
            SmokeCheck(
                name="installable_skill_registry",
                ok=installable_ok,
                detail=installable_skills,
            )
        )

        coding_profile_names = {tool.name for tool in ToolProfile.coding().filter(runtime.available_tools)}
        coding_profile_ok = {
            "code_read_file",
            "file_write",
            "file_edit",
            "file_search",
            "system_run",
            "dispatch_subagent",
        } <= coding_profile_names
        checks.append(
            SmokeCheck(
                name="coding_profile",
                ok=coding_profile_ok,
                detail=", ".join(sorted(coding_profile_names)),
            )
        )

        capabilities_json = await tool_map["capability_list_available"].handler()
        capability_status_json = await tool_map["capability_status"].handler("excel_processing")
        capability_ok = "excel_processing" in capabilities_json and "known" in capability_status_json
        checks.append(
            SmokeCheck(
                name="capability_inventory",
                ok=capability_ok,
                detail=capability_status_json,
            )
        )

        todo_result = await tool_map["todo_write"].handler([
            {"content": "准备 smoke 验证", "status": "completed"},
            {"content": "执行 smoke 验证", "status": "in_progress", "activeForm": "正在执行 smoke 验证"},
        ])
        checks.append(
            SmokeCheck(
                name="todo_planning",
                ok="[x]" in todo_result and "[>]" in todo_result,
                detail=todo_result,
            )
        )

        original_provider = runtime.subagent_runner._provider  # noqa: SLF001
        runtime.subagent_runner._provider = _SmokeProvider()  # noqa: SLF001
        try:
            subagent_result = await tool_map["dispatch_subagent"].handler(
                prompt="总结 smoke 子任务结果",
                description="smoke subagent",
            )
        finally:
            runtime.subagent_runner._provider = original_provider  # noqa: SLF001
        checks.append(
            SmokeCheck(
                name="subagent",
                ok="subagent smoke ok" in subagent_result,
                detail=subagent_result,
            )
        )

        task_create = await tool_map["task_create"].handler(subject="smoke task", description="verify dag")
        created = json.loads(task_create)
        task_id = created["id"]
        task_update = await tool_map["task_update"].handler(task_id=task_id, status="completed")
        updated = json.loads(task_update)
        task_list = await tool_map["task_list"].handler()
        task_get = await tool_map["task_get"].handler(task_id=task_id)
        dag_ok = updated["status"] == "completed" and "smoke task" in task_list and "smoke task" in task_get
        checks.append(
            SmokeCheck(
                name="task_dag",
                ok=dag_ok,
                detail=f"task #{task_id} status={updated['status']}",
            )
        )
        runtime.task_dag.delete(task_id)

        python_snippet = "print('bg smoke ok')"
        background_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(python_snippet)}"
        background_launch = await tool_map["background_run"].handler(command=background_command)
        task_id = next(reversed(runtime.background_manager._tasks))  # noqa: SLF001
        await asyncio.sleep(0.5)
        background_status = await tool_map["check_background"].handler(task_id=task_id)
        background_ok = "已启动" in background_launch and "bg smoke ok" in background_status
        checks.append(
            SmokeCheck(
                name="background_task",
                ok=background_ok,
                detail=background_status,
            )
        )
        runtime.background_manager.clear_completed()

        coding_file_path = "data/staging/smoke_coding_loop.txt"
        write_payload = json.loads(await tool_map["file_write"].handler(path=coding_file_path, content="alpha\nbeta\n"))
        read_payload = await tool_map["code_read_file"].handler(coding_file_path)
        edit_payload = json.loads(
            await tool_map["file_edit"].handler(
                path=coding_file_path,
                old_text="beta",
                new_text="gamma",
            )
        )
        reread_payload = await tool_map["code_read_file"].handler(coding_file_path)
        search_payload = json.loads(
            await tool_map["file_search"].handler(
                pattern="gamma",
                path="data/staging",
                max_results=5,
            )
        )
        python_readback = (
            "from pathlib import Path; "
            "print(Path('data/staging/smoke_coding_loop.txt').read_text().strip())"
        )
        exec_payload = json.loads(
            await tool_map["system_run"].handler(
                command=f"{shlex.quote(sys.executable)} -c {shlex.quote(python_readback)}",
                workdir=str(runtime.paths.root),
                timeout=30,
            )
        )
        coding_loop_ok = (
            write_payload.get("success") is True
            and "alpha" in read_payload
            and edit_payload.get("success") is True
            and "gamma" in reread_payload
            and search_payload.get("exit_code") == 0
            and "smoke_coding_loop.txt" in search_payload.get("matches", "")
            and "gamma" in search_payload.get("matches", "")
            and exec_payload.get("exit_code") == 0
            and "gamma" in exec_payload.get("stdout", "")
        )
        checks.append(
            SmokeCheck(
                name="coding_loop",
                ok=coding_loop_ok,
                detail=search_payload.get("matches", "")[:200],
            )
        )

        original_compressor_provider = runtime.compressor._provider  # noqa: SLF001
        original_transcript_dir = runtime.compressor._transcript_dir  # noqa: SLF001
        runtime.compressor._provider = None  # noqa: SLF001
        runtime.compressor._transcript_dir = smoke_transcript_dir  # noqa: SLF001
        try:
            compressed = await runtime.compressor.manual_compact(
                [
                    {"role": "system", "content": "你是 smoke 助手"},
                    {"role": "user", "content": "请记录一个很长的上下文" + "a" * 200},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call_1", "function": {"name": "read_vault", "arguments": "{}"}}],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "x" * 500},
                ],
                focus="保留 smoke 重点",
            )
        finally:
            runtime.compressor._provider = original_compressor_provider  # noqa: SLF001
            runtime.compressor._transcript_dir = original_transcript_dir  # noqa: SLF001
        compression_ok = len(compressed) <= 3 and "上下文已压缩" in compressed[-2]["content"]
        checks.append(
            SmokeCheck(
                name="context_compression",
                ok=compression_ok,
                detail=f"compressed_messages={len(compressed)}",
            )
        )
    finally:
        if smoke_transcript_dir.exists():
            for child in smoke_transcript_dir.iterdir():
                child.unlink()
            smoke_transcript_dir.rmdir()
        if smoke_skill_path.exists():
            for child in smoke_skill_path.iterdir():
                child.unlink()
            smoke_skill_path.rmdir()

    return checks
