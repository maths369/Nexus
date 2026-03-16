"""TaskDAG 测试 — 文件持久化的任务依赖图"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from nexus.agent.task_dag import TaskDAG


# ---------------------------------------------------------------------------
# 基本 CRUD
# ---------------------------------------------------------------------------

def test_create_task(tmp_path):
    """创建任务返回 JSON，文件持久化"""
    dag = TaskDAG(tmp_path / ".tasks")
    result = dag.create("搭建项目框架", description="初始化 repo 和 pyproject")

    task = json.loads(result)
    assert task["id"] == 1
    assert task["subject"] == "搭建项目框架"
    assert task["status"] == "pending"
    assert task["blockedBy"] == []
    assert task["blocks"] == []

    # 文件应存在
    assert (tmp_path / ".tasks" / "task_1.json").exists()


def test_create_auto_increment_id(tmp_path):
    """ID 自动递增"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务1")
    dag.create("任务2")
    dag.create("任务3")

    t3 = json.loads(dag.get(3))
    assert t3["subject"] == "任务3"


def test_get_task(tmp_path):
    """获取任务详情"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("测试任务")
    result = json.loads(dag.get(1))
    assert result["subject"] == "测试任务"


def test_get_nonexistent_task(tmp_path):
    """获取不存在的任务抛出异常"""
    dag = TaskDAG(tmp_path / ".tasks")
    with pytest.raises(ValueError, match="不存在"):
        dag.get(999)


def test_delete_task(tmp_path):
    """删除任务"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("要删的任务")
    result = dag.delete(1)
    assert "已删除" in result
    assert not (tmp_path / ".tasks" / "task_1.json").exists()


def test_delete_nonexistent_task(tmp_path):
    """删除不存在的任务"""
    dag = TaskDAG(tmp_path / ".tasks")
    result = dag.delete(999)
    assert "不存在" in result


# ---------------------------------------------------------------------------
# 状态更新
# ---------------------------------------------------------------------------

def test_update_status(tmp_path):
    """更新任务状态"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务A")
    result = json.loads(dag.update(1, status="in_progress"))
    assert result["status"] == "in_progress"

    result = json.loads(dag.update(1, status="completed"))
    assert result["status"] == "completed"


def test_update_invalid_status(tmp_path):
    """无效状态抛出异常"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务")
    with pytest.raises(ValueError, match="无效状态"):
        dag.update(1, status="done")


# ---------------------------------------------------------------------------
# 依赖关系
# ---------------------------------------------------------------------------

def test_add_blocked_by(tmp_path):
    """添加前置依赖"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务1")
    dag.create("任务2")

    result = json.loads(dag.update(2, add_blocked_by=[1]))
    assert 1 in result["blockedBy"]


def test_add_blocks_bidirectional(tmp_path):
    """添加后续依赖自动建立双向关系"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务1")
    dag.create("任务2")

    # 任务1 blocks 任务2
    dag.update(1, add_blocks=[2])

    t1 = json.loads(dag.get(1))
    t2 = json.loads(dag.get(2))
    assert 2 in t1["blocks"]
    assert 1 in t2["blockedBy"]


def test_complete_clears_dependency(tmp_path):
    """完成任务自动解除下游 blockedBy"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("前置任务")
    dag.create("后续任务")

    # 任务2 被任务1 阻塞
    dag.update(2, add_blocked_by=[1])
    t2 = json.loads(dag.get(2))
    assert 1 in t2["blockedBy"]

    # 完成任务1
    dag.update(1, status="completed")
    t2_after = json.loads(dag.get(2))
    assert 1 not in t2_after["blockedBy"]


def test_chain_dependency_propagation(tmp_path):
    """链式依赖传播: 1 → 2 → 3"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("步骤1")
    dag.create("步骤2")
    dag.create("步骤3")

    dag.update(2, add_blocked_by=[1])
    dag.update(3, add_blocked_by=[2])

    # 完成步骤1 → 步骤2 解除阻塞
    dag.update(1, status="completed")
    t2 = json.loads(dag.get(2))
    assert t2["blockedBy"] == []

    # 步骤3 仍被步骤2 阻塞
    t3 = json.loads(dag.get(3))
    assert 2 in t3["blockedBy"]

    # 完成步骤2 → 步骤3 解除阻塞
    dag.update(2, status="completed")
    t3_after = json.loads(dag.get(3))
    assert t3_after["blockedBy"] == []


def test_delete_clears_dependency(tmp_path):
    """删除任务也会清除依赖"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("前置")
    dag.create("后续")
    dag.update(2, add_blocked_by=[1])

    dag.delete(1)
    t2 = json.loads(dag.get(2))
    assert 1 not in t2["blockedBy"]


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def test_list_all(tmp_path):
    """列出所有任务"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务A")
    dag.create("任务B")
    dag.update(1, status="completed")

    listing = dag.list_all()
    assert "[x]" in listing
    assert "[ ]" in listing
    assert "1/2 已完成" in listing


def test_list_all_empty(tmp_path):
    """空列表"""
    dag = TaskDAG(tmp_path / ".tasks")
    assert dag.list_all() == "无任务。"


def test_get_ready_tasks(tmp_path):
    """获取就绪任务（无阻塞且 pending）"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("就绪任务")
    dag.create("被阻塞任务")
    dag.create("已完成任务")

    dag.update(2, add_blocked_by=[1])
    dag.update(3, status="completed")

    ready = dag.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0]["subject"] == "就绪任务"


def test_get_blocked_tasks(tmp_path):
    """获取被阻塞任务"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("任务1")
    dag.create("任务2")
    dag.update(2, add_blocked_by=[1])

    blocked = dag.get_blocked_tasks()
    assert len(blocked) == 1
    assert blocked[0]["subject"] == "任务2"


# ---------------------------------------------------------------------------
# 持久化与恢复
# ---------------------------------------------------------------------------

def test_persistence_across_instances(tmp_path):
    """新实例应恢复已有任务"""
    tasks_dir = tmp_path / ".tasks"

    dag1 = TaskDAG(tasks_dir)
    dag1.create("持久化任务")
    dag1.update(1, status="in_progress")

    # 创建新实例，应该能读取之前的任务
    dag2 = TaskDAG(tasks_dir)
    t1 = json.loads(dag2.get(1))
    assert t1["subject"] == "持久化任务"
    assert t1["status"] == "in_progress"

    # 新实例的 ID 应续接
    dag2.create("新任务")
    t2 = json.loads(dag2.get(2))
    assert t2["id"] == 2


def test_reset(tmp_path):
    """清空所有任务"""
    dag = TaskDAG(tmp_path / ".tasks")
    dag.create("A")
    dag.create("B")
    dag.create("C")

    result = dag.reset()
    assert "3" in result
    assert dag.list_all() == "无任务。"


def test_max_tasks_limit(tmp_path):
    """超过上限报错"""
    from nexus.agent.task_dag import MAX_TASKS

    dag = TaskDAG(tmp_path / ".tasks")
    # 直接设置 next_id 到上限附近
    dag._next_id = MAX_TASKS + 1

    with pytest.raises(ValueError, match="上限"):
        dag.create("超限任务")
