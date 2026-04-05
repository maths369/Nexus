"""Heartbeat engine for proactive long-running agent checks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

from nexus.agent.run import RunManager
from nexus.agent.session_manager import SessionManager
from nexus.agent.types import Run
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_store import SessionStore

logger = logging.getLogger(__name__)

_HEARTBEAT_SENDER = "__heartbeat__"
_HEARTBEAT_CHANNEL = "heartbeat"


@dataclass
class HeartbeatEvent:
    triggered: bool
    reason: str
    session_id: str | None = None
    run_id: str | None = None


class HeartbeatEngine:
    """Periodic background checker that runs an isolated heartbeat session."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        session_manager: SessionManager,
        context_window: ContextWindowManager,
        run_manager: RunManager,
        heartbeat_path: Path,
        enabled: bool = False,
        interval_minutes: int = 30,
        active_hours: str = "08:00-22:00",
        quiet_days: list[str] | None = None,
        ack_max_chars: int = 300,
        model: str | None = None,
    ) -> None:
        self._session_store = session_store
        self._session_manager = session_manager
        self._context_window = context_window
        self._run_manager = run_manager
        self._heartbeat_path = Path(heartbeat_path)
        self._enabled = bool(enabled)
        self._interval_seconds = max(60.0, float(interval_minutes or 30) * 60.0)
        self._active_hours = str(active_hours or "08:00-22:00")
        self._quiet_days = {str(day).strip().lower() for day in (quiet_days or []) if str(day).strip()}
        self._ack_max_chars = max(80, int(ack_max_chars or 300))
        self._model = str(model).strip() if model is not None and str(model).strip() else None
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if not self._enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._timer_loop(),
            name="heartbeat-engine",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def tick_once(self, *, now: datetime | None = None) -> HeartbeatEvent:
        moment = now or datetime.now()
        if not self._enabled:
            return HeartbeatEvent(triggered=False, reason="disabled")
        if not self._should_wake(moment):
            return HeartbeatEvent(triggered=False, reason="outside-active-window")
        heartbeat_text = self._read_heartbeat_md()
        if not self._has_pending_work(heartbeat_text):
            self._record_tick_metadata(moment, "no-pending-work")
            return HeartbeatEvent(triggered=False, reason="no-pending-work")

        session, _ = self._session_store.get_or_create_persistent_session(
            sender_id=_HEARTBEAT_SENDER,
            channel=_HEARTBEAT_CHANNEL,
        )
        event = HeartbeatEvent(triggered=False, reason="busy", session_id=session.session_id)
        async def _run() -> None:
            context_messages = self._context_window.build_context(session.session_id)
            tool_names = []
            self._session_manager.capture_runtime_snapshot(
                session.session_id,
                provider_name=self._model or self._run_manager._get_default_model(),  # noqa: SLF001
                context_message_count=len(context_messages),
                tool_profile="heartbeat",
                tool_names=tool_names,
                route_hint="heartbeat",
                extra={
                    "heartbeat_path": str(self._heartbeat_path),
                    "last_heartbeat_tick": moment.isoformat(),
                },
            )
            run = await self._run_manager.execute(
                session_id=session.session_id,
                task=self._build_heartbeat_task(heartbeat_text),
                context_messages=context_messages,
                model=self._model,
                channel=_HEARTBEAT_CHANNEL,
            )
            self._record_run_result(session.session_id, moment, run)
            event.triggered = True
            event.reason = run.status.value
            event.run_id = run.run_id

        accepted = await self._session_manager.enqueue_run(session.session_id, _run)
        if not accepted:
            self._record_tick_metadata(moment, "busy")
            return event
        return event

    async def _timer_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval_seconds)
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("HeartbeatEngine loop failed", exc_info=True)

    def _record_run_result(self, session_id: str, moment: datetime, run: Run) -> None:
        ack = (run.result or run.error or "").strip()
        if len(ack) > self._ack_max_chars:
            ack = f"{ack[:self._ack_max_chars].rstrip()}..."
        content = f"[heartbeat] {run.status.value}: {ack}" if ack else f"[heartbeat] {run.status.value}"
        self._session_store.add_event(
            session_id=session_id,
            role="system",
            content=content,
            metadata={
                "kind": "heartbeat",
                "run_id": run.run_id,
                "status": run.status.value,
                "timestamp": moment.isoformat(),
            },
        )
        self._session_store.update_session_metadata(
            session_id,
            {
                "heartbeat": {
                    "last_checked_at": moment.isoformat(),
                    "last_run_id": run.run_id,
                    "last_status": run.status.value,
                    "last_ack": ack,
                    "last_heartbeat_path": str(self._heartbeat_path),
                }
            },
        )

    def _record_tick_metadata(self, moment: datetime, reason: str) -> None:
        session, _ = self._session_store.get_or_create_persistent_session(
            sender_id=_HEARTBEAT_SENDER,
            channel=_HEARTBEAT_CHANNEL,
        )
        self._session_store.update_session_metadata(
            session.session_id,
            {
                "heartbeat": {
                    "last_checked_at": moment.isoformat(),
                    "last_status": reason,
                    "last_heartbeat_path": str(self._heartbeat_path),
                }
            },
        )

    def _read_heartbeat_md(self) -> str:
        if not self._heartbeat_path.exists():
            return ""
        try:
            return self._heartbeat_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Failed to read heartbeat file: %s", self._heartbeat_path, exc_info=True)
            return ""

    def _has_pending_work(self, content: str) -> bool:
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#") or line.startswith("<!--") or line.startswith("-->"):
                continue
            return True
        return False

    def _should_wake(self, moment: datetime) -> bool:
        weekday = moment.strftime("%A").strip().lower()
        if weekday in self._quiet_days:
            return False
        start_at, end_at = self._parse_active_hours()
        now_time = moment.time()
        if start_at <= end_at:
            return start_at <= now_time <= end_at
        return now_time >= start_at or now_time <= end_at

    def _parse_active_hours(self) -> tuple[time, time]:
        raw = self._active_hours.strip()
        if "-" not in raw:
            return time(8, 0), time(22, 0)
        start_raw, end_raw = raw.split("-", 1)
        return self._parse_clock(start_raw), self._parse_clock(end_raw)

    @staticmethod
    def _parse_clock(value: str) -> time:
        hour_text, minute_text = value.strip().split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))

    def _build_heartbeat_task(self, heartbeat_text: str) -> str:
        return (
            "你是 Nexus 的心跳巡检子智能体。"
            "请只根据 heartbeat.md 中的待办和上下文判断是否需要执行动作。"
            "如果没有必要动作，明确返回 HEARTBEAT_OK。"
            "如果有必要动作，请直接执行最小必要步骤，并简短说明结果。\n\n"
            f"heartbeat.md 内容如下：\n{heartbeat_text.strip()}"
        )
