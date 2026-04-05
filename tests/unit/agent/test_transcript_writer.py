from __future__ import annotations

import asyncio
import json

from nexus.agent.attempt import AttemptBuilder
from nexus.agent.run import RunManager
from nexus.agent.run_store import RunStore
from nexus.agent.transcript import TranscriptWriter
from nexus.agent.tools_policy import ToolsPolicy
from nexus.agent.types import AttemptConfig, Run, RunEvent, ToolDefinition, ToolRiskLevel
from nexus.evolution.audit import AuditLog
from nexus.evolution.sandbox import Sandbox
from nexus.evolution.skill_manager import SkillManager


def _make_attempt() -> AttemptConfig:
    return AttemptConfig(
        model="qwen3.5-plus",
        system_prompt="You are Nexus.",
        tools=[],
        messages=[{"role": "user", "content": "请总结这个任务"}],
    )


def _make_run() -> Run:
    run = Run(
        run_id="run-1",
        session_id="session-1",
        task="请总结这个任务",
        model="qwen3.5-plus",
    )
    run.result = "这是最终结果"
    return run


def test_transcript_writer_outputs_jsonl_snapshot(tmp_path):
    writer = TranscriptWriter(tmp_path / "transcripts")
    run = _make_run()
    events = [
        RunEvent(event_id="e-1", run_id=run.run_id, event_type="status_change", data={"from": "queued", "to": "running"}),
        RunEvent(event_id="e-2", run_id=run.run_id, event_type="tool_call", data={"call_id": "c-1", "tool": "read_vault", "arguments": {"path": "pages/a.md"}}),
        RunEvent(event_id="e-3", run_id=run.run_id, event_type="tool_result", data={"call_id": "c-1", "tool": "read_vault", "success": True}),
    ]

    path = writer.write_run_snapshot(
        run=run,
        attempt=_make_attempt(),
        events=events,
        tool_profile="coding",
    )

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert path == tmp_path / "transcripts" / "session-1" / "run-1.jsonl"
    assert lines[0]["kind"] == "meta"
    assert lines[0]["metadata"]["tool_profile"] == "coding"
    assert lines[1]["kind"] == "system_prompt"
    assert any(item["kind"] == "user_message" for item in lines)
    assert any(item["kind"] == "tool_call" for item in lines)
    assert any(item["kind"] == "tool_result" for item in lines)
    assert lines[-1]["kind"] == "final_output"
    assert lines[-1]["content"] == "这是最终结果"


class _FakeGateway:
    async def chat_completion(self, **kwargs):
        return {
            "message": {
                "role": "assistant",
                "content": "done",
                "tool_calls": [],
            }
        }


def test_run_manager_writes_snapshot_on_success(tmp_path):
    skills_dir = tmp_path / "skills"
    registry_dir = tmp_path / "skill_registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
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
    attempt_builder = AttemptBuilder(available_tools=[], skill_manager=skill_manager)
    writer = TranscriptWriter(tmp_path / "run_transcripts")
    run_manager = RunManager(
        run_store=RunStore(tmp_path / "runs.db"),
        attempt_builder=attempt_builder,
        provider=_FakeGateway(),
        tools_policy=ToolsPolicy(),
        fallback_models=["qwen-test"],
        skill_manager=skill_manager,
        transcript_writer=writer,
    )

    run = asyncio.run(
        run_manager.execute(
            session_id="session-xyz",
            task="写一个总结",
            context_messages=[{"role": "user", "content": "写一个总结"}],
            model="qwen-test",
        )
    )

    path = tmp_path / "run_transcripts" / "session-xyz" / f"{run.run_id}.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert run.result == "done"
    assert path.exists()
    assert any(item["kind"] == "status_change" and item["metadata"]["to"] == "succeeded" for item in lines)
