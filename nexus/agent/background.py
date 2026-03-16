"""
Background Task Manager — 异步后台执行

参考: learn-claude-code s08_background_tasks.py

核心机制:
1. Agent 通过 background_run 提交长时间运行的命令
2. 命令通过 asyncio.create_subprocess_exec 在后台执行
3. 每轮 LLM 调用前 drain notification queue，注入已完成的结果

    Main loop                 Background coroutine
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

核心洞见: "Fire and forget — Agent 不会阻塞在长时间命令上。"
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# 后台命令默认超时时间
DEFAULT_TIMEOUT = 300  # 5 分钟
MAX_BACKGROUND_TASKS = 10
TERMINAL_STATUSES = {"completed", "error", "timeout", "cancelled"}


class BackgroundTaskManager:
    """
    异步后台任务管理器。

    使用 asyncio 在后台执行 shell 命令，
    结果通过 notification queue 在每轮 LLM 调用前注入。
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._timeout = timeout
        self._asyncio_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def stats(self) -> dict[str, int]:
        running = sum(1 for t in self._tasks.values() if t["status"] == "running")
        completed = sum(1 for t in self._tasks.values() if t["status"] in ("completed", "error", "timeout"))
        return {"running": running, "completed": completed, "total": len(self._tasks)}

    # ------------------------------------------------------------------
    # 提交后台任务
    # ------------------------------------------------------------------

    async def submit(self, command: str) -> str:
        """
        提交后台命令，立即返回 task_id。

        Args:
            command: shell 命令

        Returns:
            状态消息，包含 task_id
        """
        if self._closed:
            return "Error: 后台任务管理器已关闭"
        if sum(1 for t in self._tasks.values() if t["status"] == "running") >= MAX_BACKGROUND_TASKS:
            return f"Error: 后台任务数量已达上限 ({MAX_BACKGROUND_TASKS})"

        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {
            "status": "running",
            "command": command,
            "result": None,
        }

        # 启动后台协程
        task = asyncio.create_task(self._execute(task_id, command))
        self._asyncio_tasks[task_id] = task
        task.add_done_callback(lambda _task, tid=task_id: self._asyncio_tasks.pop(tid, None))
        logger.info(f"[bg:{task_id}] Background task started: {command[:80]}")
        return f"后台任务 {task_id} 已启动: {command[:80]}"

    def _mark_task(self, task_id: str, *, status: str, result: str | None) -> None:
        self._tasks[task_id]["status"] = status
        self._tasks[task_id]["result"] = result

    def _close_process_transport(self, proc: asyncio.subprocess.Process | None) -> None:
        if proc is None:
            return
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()

    async def _execute(self, task_id: str, command: str) -> None:
        """后台执行 shell 命令"""
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._timeout,
                )
                output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
                status = "completed" if proc.returncode == 0 else "error"
                if not output:
                    output = f"(退出码: {proc.returncode})"
            except asyncio.TimeoutError:
                output = f"Error: 超时 ({self._timeout}s)"
                status = "timeout"
                self._mark_task(task_id, status=status, result=output)
                if proc.returncode is None:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.communicate(), timeout=5)

        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.communicate(), timeout=5)
            if self._tasks[task_id]["status"] not in TERMINAL_STATUSES:
                self._mark_task(task_id, status="cancelled", result="Cancelled")
            raise
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
        finally:
            self._close_process_transport(proc)

        # 更新任务状态
        self._mark_task(task_id, status=status, result=output[:50_000])

        # 推入通知队列
        async with self._lock:
            self._notifications.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": output[:500],
            })

        logger.info(f"[bg:{task_id}] Background task {status}: {command[:60]}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def check(self, task_id: str | None = None) -> str:
        """
        查询后台任务状态。

        Args:
            task_id: 指定任务 ID，None 则列出全部

        Returns:
            状态描述
        """
        if task_id:
            task = self._tasks.get(task_id)
            if not task:
                return f"Error: 未知任务 {task_id}"
            result_preview = task.get("result") or "(运行中)"
            return (
                f"[{task['status']}] {task['command'][:60]}\n"
                f"{result_preview[:500]}"
            )

        if not self._tasks:
            return "无后台任务。"

        lines: list[str] = []
        for tid, task in self._tasks.items():
            lines.append(f"{tid}: [{task['status']}] {task['command'][:60]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 通知排空（由 core.py 在每轮 LLM 调用前调用）
    # ------------------------------------------------------------------

    async def drain_notifications(self) -> list[dict[str, Any]]:
        """返回并清空已完成的通知队列"""
        async with self._lock:
            notifs = list(self._notifications)
            self._notifications.clear()
        return notifs

    def format_notifications(self, notifications: list[dict[str, Any]]) -> str:
        """将通知格式化为可注入消息的文本"""
        lines = [
            f"[bg:{n['task_id']}] {n['status']}: {n['result']}"
            for n in notifications
        ]
        return "<background-results>\n" + "\n".join(lines) + "\n</background-results>"

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def clear_completed(self) -> str:
        """清理已完成的任务记录"""
        to_remove = [
            tid for tid, t in self._tasks.items()
            if t["status"] in ("completed", "error", "timeout", "cancelled")
        ]
        for tid in to_remove:
            del self._tasks[tid]
        return f"已清理 {len(to_remove)} 个已完成任务"

    async def aclose(self) -> None:
        """取消并清理所有仍在运行的后台任务。"""
        self._closed = True
        pending = [
            task
            for tid, task in self._asyncio_tasks.items()
            if not task.done() and self._tasks.get(tid, {}).get("status") == "running"
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
