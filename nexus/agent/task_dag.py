"""
Task DAG — 文件持久化的任务依赖图

参考: learn-claude-code s07_task_system.py

核心机制:
1. 每个 task 保存为 .tasks/task_{id}.json
2. 依赖关系通过 blockedBy/blocks 双向维护
3. 完成一个 task 时自动从其他 task 的 blockedBy 中移除
4. 文件持久化意味着上下文压缩后 task 状态仍然存在

    .tasks/
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
      task_3.json  {"id":3, "blockedBy":[2], "blocks":[], ...}

    依赖自动传播:
    +----------+     +----------+     +----------+
    | task 1   | --> | task 2   | --> | task 3   |
    | complete |     | blocked  |     | blocked  |
    +----------+     +----------+     +----------+
         |                ^
         +--- completing task 1 removes it from task 2's blockedBy

核心洞见: "文件是跨压缩的持久状态 — 不会因为 context compact 而丢失。"
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_TASKS = 100


class TaskDAG:
    """
    任务依赖图管理器（JSON 文件持久化）。

    每个 task 存储为独立 JSON 文件，支持:
    - CRUD 操作
    - 双向依赖关系 (blockedBy ↔ blocks)
    - 自动依赖传播（完成时移除下游 blockedBy）
    - 就绪任务查询（blockedBy 为空且 status=pending）
    """

    def __init__(self, tasks_dir: Path):
        self._dir = tasks_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    # ------------------------------------------------------------------
    # 内部 I/O
    # ------------------------------------------------------------------

    def _max_id(self) -> int:
        """扫描已有任务文件获取最大 ID"""
        ids: list[int] = []
        for f in self._dir.glob("task_*.json"):
            try:
                ids.append(int(f.stem.split("_")[1]))
            except (IndexError, ValueError):
                pass
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict[str, Any]:
        """加载指定任务"""
        path = self._dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"任务 {task_id} 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, task: dict[str, Any]) -> None:
        """保存任务到文件"""
        path = self._dir / f"task_{task['id']}.json"
        path.write_text(
            json.dumps(task, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, subject: str, description: str = "") -> str:
        """
        创建新任务。

        Returns:
            JSON 字符串表示新任务
        """
        if self._next_id > MAX_TASKS:
            raise ValueError(f"任务数量已达上限 ({MAX_TASKS})")

        task: dict[str, Any] = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "blockedBy": [],
            "blocks": [],
        }
        self._save(task)
        self._next_id += 1
        logger.info(f"Task created: #{task['id']} - {subject}")
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        """获取任务详情"""
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(
        self,
        task_id: int,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
    ) -> str:
        """
        更新任务状态或依赖关系。

        Args:
            task_id: 任务 ID
            status: 新状态 (pending/in_progress/completed)
            add_blocked_by: 添加前置依赖
            add_blocks: 添加后续依赖

        Returns:
            更新后的任务 JSON
        """
        task = self._load(task_id)

        if status is not None:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"无效状态: {status}")
            task["status"] = status
            # 完成时自动解除其他任务的依赖
            if status == "completed":
                self._clear_dependency(task_id)
                logger.info(f"Task #{task_id} completed, dependencies cleared")

        if add_blocked_by:
            existing = set(task["blockedBy"])
            existing.update(add_blocked_by)
            task["blockedBy"] = sorted(existing)

        if add_blocks:
            existing = set(task["blocks"])
            existing.update(add_blocks)
            task["blocks"] = sorted(existing)
            # 双向维护: 也更新被阻塞任务的 blockedBy
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        blocked["blockedBy"].sort()
                        self._save(blocked)
                except ValueError:
                    pass  # 被阻塞的任务不存在，跳过

        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def delete(self, task_id: int) -> str:
        """删除任务"""
        path = self._dir / f"task_{task_id}.json"
        if not path.exists():
            return f"任务 {task_id} 不存在"
        # 先清除依赖关系
        self._clear_dependency(task_id)
        path.unlink()
        logger.info(f"Task #{task_id} deleted")
        return f"任务 #{task_id} 已删除"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_all(self) -> str:
        """列出所有任务及状态"""
        tasks = self._load_all()
        if not tasks:
            return "无任务。"

        lines: list[str] = []
        markers = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }
        for t in tasks:
            marker = markers.get(t["status"], "[?]")
            blocked = ""
            if t.get("blockedBy"):
                blocked = f" (blocked by: {t['blockedBy']})"
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")

        done = sum(1 for t in tasks if t["status"] == "completed")
        lines.append(f"\n({done}/{len(tasks)} 已完成)")
        return "\n".join(lines)

    def get_ready_tasks(self) -> list[dict[str, Any]]:
        """获取所有可执行的任务（无前置依赖且状态为 pending）"""
        tasks = self._load_all()
        return [
            t for t in tasks
            if t["status"] == "pending" and not t.get("blockedBy")
        ]

    def get_blocked_tasks(self) -> list[dict[str, Any]]:
        """获取所有被阻塞的任务"""
        tasks = self._load_all()
        return [
            t for t in tasks
            if t.get("blockedBy") and t["status"] != "completed"
        ]

    def _load_all(self) -> list[dict[str, Any]]:
        """加载所有任务"""
        tasks: list[dict[str, Any]] = []
        for f in sorted(self._dir.glob("task_*.json")):
            try:
                tasks.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return tasks

    # ------------------------------------------------------------------
    # 依赖传播
    # ------------------------------------------------------------------

    def _clear_dependency(self, completed_id: int) -> None:
        """从所有其他任务的 blockedBy 中移除已完成的任务 ID"""
        for f in self._dir.glob("task_*.json"):
            try:
                task = json.loads(f.read_text(encoding="utf-8"))
                if completed_id in task.get("blockedBy", []):
                    task["blockedBy"].remove(completed_id)
                    self._save(task)
            except Exception:
                pass

    def reset(self) -> str:
        """清空所有任务"""
        count = 0
        for f in self._dir.glob("task_*.json"):
            f.unlink()
            count += 1
        self._next_id = 1
        return f"已清空 {count} 个任务"
