"""
Todo Manager — Agent 自我进度追踪

参考: learn-claude-code s03_todo_write.py

核心机制:
1. Agent 通过 todo_write 工具管理任务清单
2. 每个任务有 pending / in_progress / completed 三种状态
3. 同一时间只能有一个 in_progress 任务
4. 如果 Agent 连续 N 轮未更新 todo，注入提醒

核心洞见: "Agent 可以追踪自己的进度——而用户也能看到它。"
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 连续 N 轮工具调用未更新 todo 后注入提醒
DEFAULT_NAG_AFTER_ROUNDS = 3
MAX_TODO_ITEMS = 20


class TodoManager:
    """
    Agent 内部进度追踪器。

    由 Agent 通过 todo_write 工具主动维护，
    核心循环每轮检查是否需要注入提醒。

    状态流转:
        pending  ──>  in_progress  ──>  completed
    """

    def __init__(self, nag_after: int = DEFAULT_NAG_AFTER_ROUNDS):
        self._items: list[dict[str, str]] = []
        self._nag_after = nag_after
        self._rounds_since_update = 0

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def items(self) -> list[dict[str, str]]:
        """返回当前 todo 列表的副本"""
        return list(self._items)

    @property
    def should_nag(self) -> bool:
        """是否应该注入提醒（有未完成任务且长时间未更新）"""
        return (
            bool(self._items)
            and self._rounds_since_update >= self._nag_after
            and any(t["status"] != "completed" for t in self._items)
        )

    @property
    def active_item(self) -> dict[str, str] | None:
        """当前正在进行的任务"""
        for item in self._items:
            if item["status"] == "in_progress":
                return item
        return None

    # ------------------------------------------------------------------
    # 核心操作
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """每轮工具调用后递增计数（由 core.py 调用）"""
        self._rounds_since_update += 1

    def update(self, items: list[dict[str, Any]]) -> str:
        """
        全量替换 todo 列表。

        Args:
            items: list of {content, status[, id, activeForm/active_form]}
                   status 可选值: "pending" | "in_progress" | "completed"

        Returns:
            渲染后的 todo 列表字符串

        Raises:
            ValueError: 验证失败时
        """
        if len(items) > MAX_TODO_ITEMS:
            raise ValueError(f"最多 {MAX_TODO_ITEMS} 个任务")

        validated: list[dict[str, str]] = []
        in_progress_count = 0

        for i, item in enumerate(items):
            content = str(item.get("content", item.get("text", ""))).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            active_form = str(
                item.get("activeForm", item.get("active_form", content))
            ).strip()

            if not content:
                raise ValueError(f"Item {item_id}: content 必填")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: 无效状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1

            validated.append({
                "id": item_id,
                "content": content,
                "status": status,
                "active_form": active_form,
            })

        if in_progress_count > 1:
            raise ValueError("同一时间只能有一个 in_progress 任务")

        self._items = validated
        self._rounds_since_update = 0
        logger.debug("TodoManager updated: %d items", len(validated))
        return self.render()

    def render(self) -> str:
        """渲染当前 todo 列表"""
        if not self._items:
            return "无任务。"

        markers = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }
        lines: list[str] = []
        for item in self._items:
            marker = markers[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['content']}")

        done = sum(1 for t in self._items if t["status"] == "completed")
        total = len(self._items)
        lines.append(f"\n({done}/{total} 已完成)")
        return "\n".join(lines)

    def get_nag_message(self) -> str:
        """生成提醒消息"""
        active = self.active_item
        if active:
            return (
                f"<reminder>你正在: {active['active_form']}。"
                f"请用 todo_write 更新进度。</reminder>"
            )
        return "<reminder>请用 todo_write 更新你的任务进度。</reminder>"

    def reset(self) -> None:
        """清空所有任务"""
        self._items.clear()
        self._rounds_since_update = 0
