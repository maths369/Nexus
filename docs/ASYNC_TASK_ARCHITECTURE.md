# Nexus 异步任务架构改造

> ADR-002: 从同步 RPC 改为异步任务模型

## 1. 问题陈述

### 1.1 当前架构（同步阻塞 RPC）

```
Desktop POST → Hub SSE → Orchestrator(持有 session 锁)
    → LLM Agent Loop → 调用 mesh_dispatch 工具
        → dispatch_to_edge()
            → MQTT 发布任务
            → await asyncio.wait_for(future, timeout=300s)  ← 阻塞
            → 等到结果才 return 给 LLM
        → LLM 生成回复 → SSE 返回
```

### 1.2 具体问题

| 问题 | 位置 | 后果 |
|------|------|------|
| `dispatch_to_edge()` 同步等待 | `remote_tools.py:185` | 整条链路阻塞最多 300 秒 |
| 等待期间零 SSE 事件 | `orchestrator.py:777-780` | 用户看到死寂，前端以为超时断开 |
| Session 锁被持有 | `orchestrator.py:714` | 用户发追问消息直接被拒 |
| dispatch future 仅在内存中 | `remote_tools.py:164` | Hub 重启 = 结果丢失 |
| 工具串行执行 | `core.py:172` | 多步骤不能并行 |
| WKWebView fetch 超时 | Desktop App | ~60s 后 "Load failed"，丢弃已执行的结果 |

### 1.3 核心错误

Hub 把"派发任务给远端节点并等待结果"当成了一次同步函数调用。
但远端节点执行时间不可预测（读邮件 2 分钟、浏览网页 5 分钟），不应该同步等待。

---

## 2. 架构概念

### 2.1 EventSource 与 Mesh Node

```
 ┌───────────────────────────────────────────────────────────────┐
 │                    Event Sources (用户交互通道)                 │
 │                                                               │
 │   Desktop UI        飞书 Channel       (未来: Telegram 等)     │
 │       │                  │                    │               │
 └───────┼──────────────────┼────────────────────┼───────────────┘
         │                  │                    │
         │ localhost        │ webhook            │
         ▼                  ▼                    ▼
 ┌───────────────────────────────────────────────────────────────┐
 │                     Mesh Network (执行层)                      │
 │                                                               │
 │   ┌──────────────┐    MQTT     ┌──────────────────┐           │
 │   │  Mac Edge    │◄═══════════►│    Hub Node      │           │
 │   │  (本机节点)   │             │   (Ubuntu 节点)   │           │
 │   │              │             │                  │           │
 │   │ • Chrome 浏览器│            │ • 知识检索        │           │
 │   │ • 本地文件    │             │ • Vault 文档      │           │
 │   │ • 系统操作    │             │ • 调度协调        │           │
 │   │ • kimi-k2.5  │             │ • qwen-plus      │           │
 │   └──────────────┘             └──────────────────┘           │
 │                                                               │
 └───────────────────────────────────────────────────────────────┘
```

**两个正交维度：**

- **EventSource（通道）**：Desktop UI、飞书 Channel、Telegram……
  - 只负责：提交用户指令、接收并展示结果
  - 每个 EventSource 有一个 **网关节点**（Desktop → Mac Edge，飞书 → Hub）
  - EventSource 不是节点，不执行任务

- **Mesh Node（节点）**：Mac Edge、Hub、未来的 iPhone 等
  - 负责：实际执行任务
  - 每个节点有独立的 LLM、工具集、能力声明
  - 节点之间通过 MQTT 协作

### 2.2 核心原则

1. **任务提交与结果获取完全解耦** — 网关节点发完 MQTT 就返回，永远不阻塞
2. **目标节点完成后主动告知** — 通过 MQTT 发布 ack/progress/result
3. **EventSource 无关性** — 同一个任务，无论从 Desktop 还是飞书发起，走相同的执行路径
4. **状态持久化** — Task 记录在 SQLite，进程重启不丢失

---

## 3. 任务生命周期

### 3.1 状态机

```
SUBMITTED ──→ DISPATCHED ──→ ACKNOWLEDGED ──→ EXECUTING ──→ COMPLETED
    │              │               │              │             │
    │              │               │              │             └→ (终态)
    │              │               │              │
    │              │               │              └→ FAILED (终态)
    │              │               │
    │              │               └→ STALE (心跳超时, 可重派)
    │              │
    │              └→ TIMED_OUT (30s 无 ACK, 可重派)
    │
    └→ REJECTED (无可用节点, 终态)
```

### 3.2 异步调用流程

```
1. EventSource 提交任务
   POST /tasks {"task": "帮我查看网易邮箱今天的邮件"}

   网关节点:
     → 创建 Task(status=SUBMITTED)
     → 判断：本地执行 or 跨节点派发？
     → 如果跨节点：MQTT publish tasks/{task_id}/dispatch
     → Task.status = DISPATCHED
     → HTTP 202 返回 {task_id}           ← 耗时 < 100ms

2. 目标节点收到任务 (MQTT)
     → MQTT publish tasks/{task_id}/ack   ← 确认收到
     → 开始执行
     → 每 15s: MQTT publish tasks/{task_id}/progress
       {"progress": 45, "message": "正在读取第3封邮件..."}

3. 网关节点 MQTT Listener (后台协程，非阻塞)
     → 收到 ack     → Task.status = ACKNOWLEDGED → 推送 EventSource
     → 收到 progress → Task.progress = 45        → 推送 EventSource
     → 收到 result   → Task.status = COMPLETED   → 推送 EventSource

4. EventSource 实时接收 (SSE / 飞书消息 / ...)
     → task_dispatched: "任务已派发到 Mac 节点"
     → task_acknowledged: "Mac 节点已确认"
     → task_progress: "正在读取第3封邮件... (45%)"
     → task_completed: "这是您的邮件报告..."
```

---

## 4. 数据模型

### 4.1 Task

```python
class TaskStatus(str, Enum):
    SUBMITTED = "submitted"
    DISPATCHED = "dispatched"
    ACKNOWLEDGED = "acknowledged"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    STALE = "stale"
    REJECTED = "rejected"

@dataclass
class Task:
    task_id: str                    # UUID
    session_id: str                 # 关联的会话
    source_type: str                # "desktop" | "feishu" | "telegram"
    source_id: str                  # EventSource 实例标识
    gateway_node: str               # 网关节点 ID
    executor_node: str | None       # 实际执行节点 ID
    task_description: str           # 任务描述
    status: TaskStatus
    progress: int                   # 0-100
    progress_message: str           # "正在读取第3封邮件..."
    result: str | None              # 最终结果
    error: str | None               # 错误信息
    attempt: int                    # 当前重试次数
    max_retries: int                # 最大重试 (默认 1)
    created_at: float
    dispatched_at: float | None
    acknowledged_at: float | None
    completed_at: float | None
    timeout_seconds: float          # 默认 600
```

### 4.2 EventSource

```python
class EventSource(ABC):
    """所有用户交互通道的基类"""
    source_type: str           # "desktop" | "feishu" | "telegram"
    source_id: str             # 唯一标识
    gateway_node: str          # 网关节点 ID

    @abstractmethod
    async def push_event(self, event: TaskEvent) -> None:
        """推送事件给用户"""
        ...

class DesktopSource(EventSource):
    """Desktop UI — 通过 SSE 推送"""
    source_type = "desktop"
    gateway_node = "macbook-pro-yanglei"

    async def push_event(self, event: TaskEvent):
        await self.sse_queue.put(event)

class FeishuSource(EventSource):
    """飞书 Channel — 通过飞书 Bot API 推送"""
    source_type = "feishu"
    gateway_node = "hub-ubuntu"

    async def push_event(self, event: TaskEvent):
        await feishu_bot.send_message(
            chat_id=self.chat_id,
            content=event.to_feishu_card()
        )
```

### 4.3 TaskEvent

```python
@dataclass
class TaskEvent:
    task_id: str
    event_type: str       # "dispatched" | "acknowledged" | "progress" | "completed" | "failed"
    content: str          # 可展示给用户的文本
    progress: int | None  # 0-100, 仅 progress 事件
    metadata: dict        # 附加信息
    timestamp: float
```

---

## 5. MQTT Topic 设计

```
tasks/{task_id}/dispatch     # 网关 → 目标节点：发布任务
tasks/{task_id}/ack          # 目标节点 → 网关：确认收到
tasks/{task_id}/progress     # 目标节点 → 网关：进度更新 (每 15s)
tasks/{task_id}/result       # 目标节点 → 网关：最终结果
nodes/{node_id}/status       # 节点心跳 / 上下线通知
```

**设计决策：**
- 用 `task_id` 做 topic 前缀（而非 `node_id`），每个 task 有自己的消息通道
- 网关节点 subscribe `tasks/{task_id}/#` 监听特定任务的所有事件
- 目标节点 subscribe `tasks/+/dispatch` 接收新任务（按 node_id 过滤 payload）
- QoS 1 保证至少送达一次

---

## 6. 容错策略

| 场景 | 检测方式 | 处理策略 |
|------|----------|----------|
| 目标节点未 ACK | 30s 定时检查 | 标记 TIMED_OUT，可重派到其他节点 |
| 执行中无心跳 | 2 × heartbeat_interval 未收到 progress | 标记 STALE，可重派 |
| Edge 进程崩溃 | MQTT Last Will 消息 | 标记该节点所有进行中任务为 FAILED |
| Hub 重启 | TaskStore 持久化在 SQLite | 恢复后扫描未完成任务，重新订阅 |
| 重复结果到达 | 检查 task.status == COMPLETED | 幂等忽略 |
| 网络闪断恢复 | MQTT retained message + clean_session=False | 自动重连后收到离线期间的消息 |

---

## 7. 改造计划

### 7.1 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `nexus/mesh/task_store.py` | **新增** | Task 数据模型 + SQLite 持久化 + 状态机 |
| `nexus/mesh/event_source.py` | **新增** | EventSource 抽象基类 + Desktop/Feishu 实现 |
| `nexus/mesh/task_manager.py` | **新增** | 异步任务管理器：提交、派发、监听结果、超时检查 |
| `nexus/mesh/remote_tools.py` | **改造** | `dispatch_to_edge()` 改为 fire-and-forget |
| `nexus/orchestrator.py` | **改造** | 去掉同步等待，接入 TaskManager |
| `nexus/edge/macos_sidecar.py` | **改造** | 执行时发 ack → progress → result |
| `nexus/api/app.py` | **改造** | 新增 `POST /tasks` + `GET /tasks/{id}/stream` |
| `apps/desktop/src/lib/hub.ts` | **改造** | 用 EventSource 接收任务状态推送 |

### 7.2 实施顺序

1. **Phase 1 — 基础设施**
   - 实现 `task_store.py`（Task 模型 + SQLite CRUD）
   - 实现 `event_source.py`（EventSource 抽象 + Desktop 实现）
   - 实现 `task_manager.py`（核心调度逻辑）

2. **Phase 2 — Edge 端改造**
   - `macos_sidecar.py`: 任务执行时发布 ack/progress/result 到 MQTT
   - 保持 `/local-command` 兼容（本地直接执行不走 MQTT）

3. **Phase 3 — Hub 端改造**
   - `remote_tools.py`: dispatch 改为 fire-and-forget
   - `orchestrator.py`: 去掉同步等待，接入 TaskManager
   - `app.py`: 新增异步任务 API

4. **Phase 4 — 前端改造**
   - `hub.ts`: 统一为 EventSource SSE 模式
   - 进度条 UI 展示

---

## 8. 向后兼容

- `/local-command` 端点保留，用于 Mac 本地直接执行（不经过 MQTT）
- `/hub/desktop/message` 端点保留，但内部改为提交异步任务
- 飞书 webhook 端点保留，内部同样提交异步任务
- 旧的同步 `dispatch_to_edge()` 保留为 `dispatch_to_edge_sync()` 供测试使用
