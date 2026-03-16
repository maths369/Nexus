"""
Agent Core — 工具调用循环与 Run 状态机

参考 OpenClaw 的 run.ts + attempt.ts 分层模式:
  - core.py:       基本循环（LLM调用 → 工具执行 → 结果回传）
  - run.py:        任务编排（重试预算、模型故障切换、上下文溢出处理）
  - attempt.py:    单次执行准备（工具集、系统提示、上下文、StreamFn 适配）
  - compressor.py: 三层上下文压缩（micro/auto/manual compact）
  - todo.py:       Agent 自我进度追踪（TodoManager + nag reminder）
  - subagent.py:   子任务委派（隔离 context，summary-only 返回）
  - task_dag.py:   任务依赖图（JSON 文件持久化，blockedBy/blocks）
  - background.py: 异步后台任务（notification queue + drain before LLM call）

不使用 LangGraph。理由:
1. 执行流程本质是线性的，不是有条件分支的 DAG
2. OpenClaw（76k 行）和 NanoClaw 都不用 LangGraph
3. LangGraph 依赖链重，引入后难以移除
"""

from .background import BackgroundTaskManager
from .compressor import ContextCompressor
from .subagent import SubagentRunner
from .task_dag import TaskDAG
from .todo import TodoManager

__all__ = [
    "BackgroundTaskManager",
    "ContextCompressor",
    "SubagentRunner",
    "TaskDAG",
    "TodoManager",
]
