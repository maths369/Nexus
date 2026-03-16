from __future__ import annotations

from argparse import Namespace
import asyncio

from nexus.__main__ import cmd_chat
from nexus.agent.run_store import RunStore
from nexus.agent.types import Run, RunStatus
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_store import SessionStatus, SessionStore
from nexus.channel.types import OutboundMessage, OutboundMessageType


class _FakeBrowserService:
    async def aclose(self) -> None:
        return None


class _FakeRuntime:
    def __init__(self, session_store=None, context_window=None, run_store=None) -> None:
        self.browser_service = _FakeBrowserService()
        self.session_store = session_store
        self.context_window = context_window
        self.run_store = run_store


class _FakeOrchestrator:
    async def handle_message(self, inbound, reply_fn) -> None:
        await reply_fn(
            OutboundMessage(
                session_id="cli-session",
                message_type=OutboundMessageType.ACK,
                content=f"收到，正在处理：{inbound.content}",
            )
        )
        await reply_fn(
            OutboundMessage(
                session_id="cli-session",
                message_type=OutboundMessageType.RESULT,
                content=f"已完成：{inbound.content}",
            )
        )


def test_cmd_chat_one_shot_prints_replies(monkeypatch, capsys):
    monkeypatch.setattr(
        "nexus.__main__._build_orchestrator",
        lambda: (_FakeRuntime(), _FakeOrchestrator()),
    )

    cmd_chat(Namespace(sender_id="cli-user", message="帮我写一份测试计划"))

    out = capsys.readouterr().out
    assert "[ACK]" in out
    assert "收到，正在处理：帮我写一份测试计划" in out
    assert "[RESULT]" in out
    assert "已完成：帮我写一份测试计划" in out


def test_cmd_chat_status_history_and_clear_use_real_session_state(
    monkeypatch, capsys, tmp_path
):
    session_store = SessionStore(tmp_path / "sessions.db")
    context_window = ContextWindowManager(session_store=session_store)
    run_store = RunStore(tmp_path / "runs.db")
    session = session_store.create_session(
        sender_id="cli-user",
        channel="web",
        summary="帮我生成飞书 API 传输方案",
    )
    session_store.add_event(session.session_id, "user", "帮我生成飞书 API 传输方案")
    session_store.add_event(session.session_id, "assistant", "已生成初稿，正在校验")
    asyncio.run(
        run_store.save_run(
            Run(
                run_id="run-1",
                session_id=session.session_id,
                status=RunStatus.RUNNING,
                task="帮我生成飞书 API 传输方案",
                model="kimi-k2.5",
            )
        )
    )
    runtime = _FakeRuntime(
        session_store=session_store,
        context_window=context_window,
        run_store=run_store,
    )
    monkeypatch.setattr(
        "nexus.__main__._build_orchestrator",
        lambda: (runtime, _FakeOrchestrator()),
    )

    cmd_chat(Namespace(sender_id="cli-user", message="/status"))
    status_out = capsys.readouterr().out
    assert "当前活跃会话" in status_out
    assert "run-1 [running]" in status_out
    assert "帮我生成飞书 API 传输方案" in status_out

    cmd_chat(Namespace(sender_id="cli-user", message="/history"))
    history_out = capsys.readouterr().out
    assert "最近会话历史" in history_out
    assert session.session_id in history_out
    assert "当前活跃会话最近事件" in history_out

    cmd_chat(Namespace(sender_id="cli-user", message="/clear"))
    clear_out = capsys.readouterr().out
    assert "已清理当前会话" in clear_out
    assert session_store.get_session(session.session_id).status == SessionStatus.ABANDONED
