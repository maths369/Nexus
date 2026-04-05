from __future__ import annotations

import asyncio
from datetime import datetime

from nexus.agent.heartbeat import HeartbeatEngine
from nexus.agent.session_manager import SessionManager
from nexus.agent.types import Run, RunStatus
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore


class _FakeRunManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return Run(
            run_id="heartbeat-run",
            session_id=kwargs["session_id"],
            status=RunStatus.SUCCEEDED,
            task=kwargs["task"],
            result="HEARTBEAT_OK",
            model=kwargs.get("model") or "qwen-max",
        )

    def _get_default_model(self) -> str:
        return "qwen-max"


def test_heartbeat_engine_skips_outside_active_window(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    session_manager = SessionManager(store, router)
    heartbeat_file = tmp_path / "vault" / "_system" / "heartbeat.md"
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text("待处理事项：检查日报。", encoding="utf-8")
    run_manager = _FakeRunManager()
    engine = HeartbeatEngine(
        session_store=store,
        session_manager=session_manager,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        heartbeat_path=heartbeat_file,
        enabled=True,
        active_hours="08:00-22:00",
    )

    result = asyncio.run(engine.tick_once(now=datetime(2026, 3, 29, 23, 0)))

    assert result.triggered is False
    assert result.reason == "outside-active-window"
    assert run_manager.calls == []


def test_heartbeat_engine_runs_isolated_session_when_pending_work_exists(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    router = SessionRouter(store, ContextWindowManager(store))
    session_manager = SessionManager(store, router)
    heartbeat_file = tmp_path / "vault" / "_system" / "heartbeat.md"
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text("待处理事项：整理昨天的未完成 TODO。", encoding="utf-8")
    run_manager = _FakeRunManager()
    engine = HeartbeatEngine(
        session_store=store,
        session_manager=session_manager,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        heartbeat_path=heartbeat_file,
        enabled=True,
        active_hours="08:00-22:00",
        ack_max_chars=120,
    )

    result = asyncio.run(engine.tick_once(now=datetime(2026, 3, 29, 9, 30)))

    assert result.triggered is True
    assert result.run_id == "heartbeat-run"
    assert len(run_manager.calls) == 1
    call = run_manager.calls[0]
    assert call["model"] is None
    session = store.get_most_recent_session("__heartbeat__")
    assert session is not None
    assert call["session_id"] == session.session_id
    events = store.get_events(session.session_id)
    assert any("HEARTBEAT_OK" in event.content for event in events)
    metadata = store.get_session(session.session_id).metadata["heartbeat"]
    assert metadata["last_status"] == RunStatus.SUCCEEDED.value

