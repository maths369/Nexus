from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nexus.agent.types import Run, RunEvent, RunStatus
from nexus.evolution import CapabilityPromotionAdvisor


def _make_run(task: str) -> Run:
    run = Run(
        run_id="run-promo",
        session_id="session-promo",
        task=task,
        model="qwen-max",
        status=RunStatus.SUCCEEDED,
    )
    now = datetime.now(timezone.utc)
    run.created_at = now - timedelta(minutes=2)
    run.updated_at = now
    return run


def test_suggest_returns_none_without_tool_calls():
    advisor = CapabilityPromotionAdvisor()
    run = _make_run("以后默认支持读取 PPT 文件")

    suggestion = advisor.suggest(run=run, events=[])

    assert suggestion is None


def test_suggest_returns_none_without_permanence_intent():
    advisor = CapabilityPromotionAdvisor()
    run = _make_run("帮我临时读取这个 PPT 文件")
    events = [
        RunEvent(
            event_id="evt1",
            run_id=run.run_id,
            event_type="tool_call",
            data={"tool": "system_run", "arguments": {"command": "pip install python-pptx>=1.0.0"}},
        )
    ]

    suggestion = advisor.suggest(run=run, events=events)

    assert suggestion is None


def test_suggest_builds_capability_promotion_from_system_run():
    advisor = CapabilityPromotionAdvisor()
    run = _make_run("以后都默认支持读取 PPT 文件，并把它注册成正式能力")
    events = [
        RunEvent(
            event_id="evt1",
            run_id=run.run_id,
            event_type="tool_call",
            data={"tool": "system_run", "arguments": {"command": "pip install python-pptx>=1.0.0 pandas>=2.2.0"}},
        )
    ]

    suggestion = advisor.suggest(run=run, events=events)

    assert suggestion is not None
    assert suggestion.capability_id == "ppt_processing"
    assert "python-pptx>=1.0.0" in suggestion.proposed_packages
    assert "pandas>=2.2.0" in suggestion.proposed_packages
    assert "pptx" in suggestion.proposed_imports
