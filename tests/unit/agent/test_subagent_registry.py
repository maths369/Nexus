from __future__ import annotations

from nexus.agent.subagent_registry import SubagentRegistry


def test_subagent_registry_persists_completed_record(tmp_path):
    registry = SubagentRegistry(tmp_path / "subagents")
    registry.register_spawn(
        run_id="sub-d1-abc123",
        prompt="分析测试失败原因",
        description="分析单测",
        spawn_mode="session",
        model="qwen-max",
        parent_session_id="sess-parent",
    )
    registry.mark_running("sub-d1-abc123", attempts=1, session_id="sess-child")
    registry.mark_completed("sub-d1-abc123", result="已经分析完成", session_id="sess-child")

    record = registry.get("sub-d1-abc123")
    assert record.status == "completed"
    assert record.session_id == "sess-child"
    assert record.result == "已经分析完成"
    notifications = registry.drain_notifications()
    assert notifications[0]["parent_session_id"] == "sess-parent"


def test_subagent_registry_recovers_orphans(tmp_path):
    registry = SubagentRegistry(tmp_path / "subagents")
    registry.register_spawn(
        run_id="sub-d1-orphan",
        prompt="长时运行",
        spawn_mode="run",
        model="qwen-max",
    )
    registry.mark_running("sub-d1-orphan", attempts=1)

    recovered = registry.recover_orphans()

    assert len(recovered) == 1
    assert recovered[0].orphaned is True
    assert registry.get("sub-d1-orphan").status == "failed"

