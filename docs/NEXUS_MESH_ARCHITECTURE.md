# Nexus Mesh — Multi-Node Distributed AI Assistant Architecture

> 星策网络：跨 Ubuntu / MacBook Pro / iPhone 的协同 AI 助手架构设计

## 0. 设计动因

当前 Nexus 是一个单节点系统，运行在 Ubuntu 上。但实际使用中存在三种典型的物理节点（Hub，个人工作用笔记本，以及手机），各有优势和限制：

| 节点 | 优势 | 限制 |
|------|------|------|
| **Ubuntu Server** (Hub) | 7x24 在线、大硬盘、GPU 推理、知识库 | 无浏览器 GUI、无本地文件交互 |
| **MacBook Pro** | 浏览器、本地文件、录音录视频、主力交互界面 | CPU/GPU/内存紧张、间歇离线 |
| **iPhone** | 随身携带、拍照、录音、录像、位置信息 | 算力极弱、屏幕小、只能做轻量交互 |

核心诉求：**这三个节点组成一个网络，每个节点的独特能力可被发现和调用，任务按节点特性智能分配，每个节点都能独立通过 API 接入 LLM 处理分配给自己的任务。**

---

## 1. 核心设计原则

### 1.1 每个节点都是一个完整的 Agent Runtime

不是"一个中心 + N 个哑终端"。每个节点：
- 有自己的 LLM API 接入能力（通过 ProviderGateway）
- 默认假设都联网，`API LLM` 是各节点的基础能力
- 有自己的本地 Tool Registry（注册该节点独有的能力）
- 能独立执行分配给自己的任务
- 在与网络断开时仍可独立工作（降级模式）

补充约束：
- `本地 LLM` 不是每个节点的默认配置，而是硬件条件满足时才注册的特殊能力
- `Hub` 负责全局任务分解和跨节点编排；节点本地的 LLM 主要用于节点内的受限决策和结果整理，而不是自由改写全局计划

### 1.2 Hub-Spoke + Peer-to-Peer 混合拓扑

```
                    ┌─────────────────────┐
                    │   Ubuntu Server     │
                    │   (Hub / Always-On) │
                    │                     │
                    │  - Mesh Registry    │
                    │  - Task Scheduler   │
                    │  - Knowledge Store  │
                    │  - Local LLM        │
                    │  - MQTT Broker      │
                    └──────┬──────┬───────┘
                           │      │
              ┌────────────┘      └────────────┐
              │                                │
     ┌────────▼────────┐             ┌─────────▼────────┐
     │  MacBook Pro     │             │  iPhone           │
     │  (Edge Node)     │             │  (Mobile Node)    │
     │                  │◄───────────►│                   │
     │  - Browser       │  (optional  │  - Camera/Mic     │
     │  - Local Files   │   P2P when  │  - Location       │
     │  - Screen Cap    │   on same   │  - Push Notif     │
     │  - Dev Tools     │   network)  │  - Light Input    │
     └─────────────────┘              └──────────────────┘
```

- **Hub（Ubuntu）**：总是在线，承担 Mesh Registry、Task Scheduler、Knowledge Store、MQTT Broker
- **Spoke（MacBook / iPhone）**：连接到 Hub，注册自己的能力，接收分配的任务
- **P2P**：同一局域网内，MacBook 和 iPhone 可以直接通信（可选优化）

### 1.3 离线优先 + 最终一致

MacBook 合盖、iPhone 离开 WiFi 时，任务不应中断：
- Hub 上的长期任务继续运行
- 需要离线节点参与的任务进入 `waiting_for_node` 状态
- 节点重新上线后，自动恢复被阻塞的任务
- 在离线期间，节点可以用自己的 LLM API 接入能力独立处理本地任务

### 1.4 能力驱动的任务路由

任务分配不是硬编码到节点，而是基于：
1. 该任务需要什么能力（capability requirements）
2. 哪些节点当前在线且提供该能力
3. 节点当前负载和延迟
4. 任务的时效性要求

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Nexus Mesh Network                          │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Communication Layer                      │   │
│  │                                                             │   │
│  │   MQTT Broker (Ubuntu)  ←→  MQTT Clients (Mac/iPhone)      │   │
│  │   Topics: nexus/{node_id}/+, nexus/broadcast/+             │   │
│  │   Transport: TLS over TCP / WebSocket / QUIC               │   │
│  └──────────────┬──────────────────────────────────────────────┘   │
│                 │                                                   │
│  ┌──────────────▼──────────────────────────────────────────────┐   │
│  │                    Coordination Layer                        │   │
│  │                                                             │   │
│  │   ┌──────────────┐  ┌──────────────┐  ┌───────────────┐    │   │
│  │   │ Mesh Registry │  │ Task Router  │  │ State Sync    │    │   │
│  │   │              │  │              │  │               │    │   │
│  │   │ - Node Cards │  │ - Capability │  │ - Task State  │    │   │
│  │   │ - Capability │  │   Matching   │  │ - Session     │    │   │
│  │   │   Catalog    │  │ - Load-Aware │  │   Continuity  │    │   │
│  │   │ - Health     │  │   Routing    │  │ - Knowledge   │    │   │
│  │   │   Monitor    │  │ - Fallback   │  │   Replication │    │   │
│  │   └──────────────┘  └──────────────┘  └───────────────┘    │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Node Runtime Layer (per node)                  │   │
│  │                                                             │   │
│  │   ┌──────────────┐  ┌──────────────┐  ┌───────────────┐    │   │
│  │   │ Local Agent  │  │ Local Tool   │  │ Local Provider│    │   │
│  │   │ Runtime      │  │ Registry     │  │ Gateway       │    │   │
│  │   │              │  │              │  │               │    │   │
│  │   │ - Orchestr.  │  │ - Platform   │  │ - API LLM    │    │   │
│  │   │ - Session    │  │   Tools      │  │ - Local LLM  │    │   │
│  │   │ - Run Ctrl   │  │ - Remote     │  │   (Ubuntu)    │    │   │
│  │   │              │  │   Proxies    │  │ - Fallback    │    │   │
│  │   └──────────────┘  └──────────────┘  └───────────────┘    │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 核心组件设计

### 3.1 Node Card — 节点身份与能力声明

每个节点启动时向 Mesh Registry 注册一张 Node Card（类比 A2A 的 Agent Card）。

```yaml
# Node Card for MacBook Pro
node_id: "macbook-pro-user"
node_type: "edge"           # hub | edge | mobile
display_name: "MacBook Pro"
platform: "macos"
arch: "arm64"

# LLM 能力
providers:
  - name: "kimi"
    model: "kimi-k2.5"
    via: "api"                          # api | local
  - name: "qwen"
    model: "qwen3.5-397b-a17b"
    via: "api"

# 本地能力
capabilities:
  - capability_id: "browser_automation"
    description: "Playwright 浏览器自动化，支持导航、截图、表单填写、内容抓取"
    requires_user_interaction: true      # 某些操作需要用户手动登录
    tools: ["browser_navigate", "browser_extract_text", "browser_screenshot", "browser_fill_form"]

  - capability_id: "local_filesystem"
    description: "访问 macOS 本地文件系统"
    tools: ["list_local_files", "code_read_file"]

  - capability_id: "screen_capture"
    description: "macOS 屏幕录制和截图"
    tools: ["capture_screen", "record_screen"]

  - capability_id: "audio_recording"
    description: "macOS 麦克风录音"
    tools: ["record_audio", "stop_recording"]

  - capability_id: "video_capture"
    description: "macOS 摄像头录像"
    tools: ["capture_video"]

  - capability_id: "clipboard"
    description: "macOS 剪贴板读写"
    tools: ["read_clipboard", "write_clipboard"]

  - capability_id: "notifications"
    description: "macOS 系统通知"
    tools: ["send_notification"]

  - capability_id: "apple_shortcuts"
    description: "调用 Apple Shortcuts 自动化"
    tools: ["run_shortcut", "list_shortcuts"]

# 资源约束
resources:
  cpu_cores: 10
  memory_gb: 16
  gpu: "Laptop GPU"
  gpu_memory_gb: 0       # 共享内存，不单独计
  disk_free_gb: 120
  battery_powered: true   # 电池供电可能影响任务调度

# 可用性
availability:
  schedule: "weekdays 08:00-23:00"    # 大致可用时段
  intermittent: true                   # 可能随时离线
  max_task_duration_seconds: 1800      # 不适合超长任务
```

```yaml
# Node Card for Ubuntu Server
node_id: "ubuntu-server-hub"
node_type: "hub"
display_name: "Ubuntu Server"
platform: "linux"
arch: "x86_64"

providers:
  - name: "kimi"
    model: "kimi-k2.5"
    via: "api"
  - name: "qwen"
    model: "qwen3.5-397b-a17b"
    via: "api"
  - name: "ollama-qwen"
    model: "qwen2.5:72b"
    via: "local"                        # 本地 LLM
    gpu: "Server GPU"
    context_length: 32768

capabilities:
  - capability_id: "knowledge_store"
    description: "大规模知识库存储、检索、向量化"
    tools: ["read_vault", "write_vault", "search_vault", "knowledge_ingest"]

  - capability_id: "local_llm_inference"
    description: "本地 LLM 推理（GPU 加速）"
    tools: ["local_llm_generate"]
    exclusive: true                     # 敏感数据不出本地

  - capability_id: "audio_transcription"
    description: "SenseVoice 语音转录（GPU 加速）"
    tools: ["audio_transcribe_path"]

  - capability_id: "long_running_analysis"
    description: "长时间运行的数据分析和处理任务"
    tools: ["background_run"]

  - capability_id: "document_management"
    description: "文档创建、编辑、结构化管理"
    tools: ["create_note", "document_append_block", "document_replace_section"]

  - capability_id: "evolution_runtime"
    description: "受控自我进化：技能管理、能力注册、沙箱验证"
    tools: ["skill_install", "capability_register", "system_run"]

resources:
  cpu_cores: 32
  memory_gb: 128
  gpu: "Server GPU"
  gpu_memory_gb: 32
  disk_free_gb: 4000

availability:
  schedule: "24/7"
  intermittent: false
  max_task_duration_seconds: 0
```

```yaml
# Node Card for iPhone
node_id: "iphone-user"
node_type: "mobile"
display_name: "iPhone"
platform: "ios"
arch: "arm64"

providers:
  - name: "kimi"
    model: "kimi-k2.5"
    via: "api"

capabilities:
  - capability_id: "camera_photo"
    description: "iPhone 拍照"
    tools: ["take_photo", "photo_from_library"]
    requires_user_interaction: true

  - capability_id: "camera_video"
    description: "iPhone 录像"
    tools: ["record_video_ios"]
    requires_user_interaction: true

  - capability_id: "microphone"
    description: "iPhone 录音"
    tools: ["record_audio_ios"]
    requires_user_interaction: true

  - capability_id: "location"
    description: "GPS 定位"
    tools: ["get_location", "get_address"]

  - capability_id: "push_notification"
    description: "推送通知给用户"
    tools: ["push_notify"]

  - capability_id: "health_data"
    description: "HealthKit 健康数据"
    tools: ["read_health_data"]

  - capability_id: "contacts"
    description: "通讯录访问"
    tools: ["search_contacts"]

  - capability_id: "result_display"
    description: "向用户展示分析结果、报告、图表"
    tools: ["display_card", "display_report"]

resources:
  cpu_cores: 6
  memory_gb: 8
  battery_powered: true

availability:
  schedule: "24/7"
  intermittent: true                   # 联网不等于后台长连；按 push/前台触发建模
  max_task_duration_seconds: 300       # 只做轻量任务
  preferred_role: "input_output"       # 主要做输入采集和结果展示
```

### 3.2 Mesh Registry — 分布式能力注册表

运行在 Ubuntu Hub 上，管理所有节点的 Node Card 和能力索引。

```python
# nexus/mesh/registry.py

@dataclass
class NodeStatus:
    node_id: str
    online: bool
    last_heartbeat: datetime
    current_load: float          # 0.0 ~ 1.0
    active_tasks: int
    battery_level: float | None  # 移动设备

@dataclass
class CapabilityEntry:
    capability_id: str
    node_id: str
    tools: list[str]
    requires_user_interaction: bool
    exclusive: bool              # 是否只能在该节点执行
    estimated_latency_ms: int

class MeshRegistry:
    """
    中心化能力注册表（运行在 Hub 上）。

    职责：
    1. 接收节点注册/注销
    2. 维护节点健康状态（心跳）
    3. 提供能力查询：给定需求，返回可用节点列表
    4. 广播能力变更通知
    """

    async def register_node(self, node_card: NodeCard) -> None:
        """节点上线时注册"""

    async def deregister_node(self, node_id: str) -> None:
        """节点离线时注销"""

    async def heartbeat(self, node_id: str, status: NodeStatus) -> None:
        """节点心跳，更新状态"""

    async def query_capability(
        self,
        capability_id: str,
        *,
        prefer_online: bool = True,
        exclude_nodes: list[str] | None = None,
    ) -> list[CapabilityEntry]:
        """查询提供某能力的所有节点，按优先级排序"""

    async def resolve_tools(
        self,
        required_tools: list[str],
    ) -> dict[str, list[str]]:
        """
        给定一组工具需求，返回 {node_id: [tools]} 的分配方案。
        同一个工具如果多个节点都有，按优先级选择。
        """

    async def on_capability_changed(
        self,
        node_id: str,
        added: list[CapabilityEntry],
        removed: list[str],
    ) -> None:
        """节点能力变更通知（例如新安装了一个 Skill）"""
```

### 3.3 Task Router — 能力感知的任务路由器

这是当前 Nexus 的 `Orchestrator` 的扩展，增加跨节点任务分配能力。

```python
# nexus/mesh/task_router.py

@dataclass
class TaskPlan:
    """一个任务的跨节点执行计划"""
    task_id: str
    steps: list[TaskStep]

@dataclass
class TaskStep:
    step_id: str
    description: str
    required_capabilities: list[str]
    execution_payload: dict          # Hub 下发的结构化执行描述（tool calls / constraints / inputs）
    assigned_node: str | None       # 路由结果
    depends_on: list[str]           # 前序步骤
    timeout: timedelta
    retry_policy: RetryPolicy
    state: StepState                # pending | assigned | running | waiting_for_node | completed | failed

class TaskRouter:
    """
    能力感知的跨节点任务路由器。

    工作流程：
    1. 接收用户任务
    2. LLM 分解任务为步骤（利用 Mesh Registry 的能力目录作为上下文）
    3. 每个步骤匹配到最佳节点
    4. 下发执行指令
    5. 监控状态，处理节点离线重路由
    """

    def __init__(
        self,
        registry: MeshRegistry,
        provider: ProviderGateway,
    ):
        self._registry = registry
        self._provider = provider

    async def plan_task(self, task: str, context: list[dict]) -> TaskPlan:
        """
        用 LLM 分解任务，并基于当前 Mesh 能力进行路由。

        System Prompt 会包含：
        1. 当前在线节点及其能力列表
        2. 节点负载状态
        3. 节点可用性约束（如 MacBook 不适合长任务）
        """
        # 获取当前 Mesh 状态
        mesh_context = await self._build_mesh_context()

        # LLM 规划
        plan_prompt = f"""
你是一个多节点 AI 助手网络的任务规划器。

当前网络中的节点和能力：
{mesh_context}

用户任务：{task}

请将任务分解为步骤，并为每个步骤标注：
1. 需要的能力 (capability_ids)
2. 推荐的执行节点 (node_id)
3. 步骤间的依赖关系
4. 是否需要用户交互

分解原则：
- 需要浏览器/本地文件的步骤 → MacBook
- 需要长时间运算/大量存储的步骤 → Ubuntu Server
- 需要拍照/录音/推送通知的步骤 → iPhone
- 如果某节点离线，标注 waiting_for_node
- 每个步骤都应该有明确的输入和输出
- 输出中必须包含结构化的 execution_payload，节点不得自由改写全局计划
"""
        # ... LLM 调用并解析为 TaskPlan

    async def assign_step(self, step: TaskStep) -> str:
        """
        为单个步骤分配最佳节点。

        优先级策略：
        1. 必须匹配能力
        2. 优先选择在线节点
        3. 负载低的优先
        4. 延迟低的优先
        5. 非电池供电的优先（长任务）
        """
        candidates = []
        for cap_id in step.required_capabilities:
            entries = await self._registry.query_capability(cap_id)
            candidates.extend(entries)

        if not candidates:
            step.state = StepState.WAITING_FOR_NODE
            return ""

        # 排序策略
        scored = self._score_candidates(candidates, step)
        best = scored[0]
        step.assigned_node = best.node_id
        step.state = StepState.ASSIGNED
        return best.node_id

    async def handle_node_offline(self, node_id: str) -> None:
        """
        节点离线时的处理策略：
        1. 该节点上正在执行的任务 → 检查是否可迁移到其他节点
        2. 不可迁移的 → 标记为 waiting_for_node
        3. 可迁移的 → 重新路由
        """

    async def handle_node_online(self, node_id: str) -> None:
        """
        节点上线时：
        1. 恢复该节点的 waiting_for_node 任务
        2. 检查新能力是否能解决其他被阻塞的任务
        """
```

约束：
- `Hub` 拥有全局任务分解权和节点分配权
- 节点本地的 API LLM 可以用于受限范围内的本地推理，例如参数补全、页面摘要、局部 UI 选择
- 节点不得在未获授权的情况下新增步骤、改写节点分配、扩大工具集合
- 当前实现里，`TaskRouter` 会在 run 前就决定每一步属于哪个节点
- 对于远端步骤，只注入该节点的远端工具；如果 Hub 本地也存在同名工具，则在本轮 run 中显式禁用 Hub 本地版本
- 传给 Agent 的任务文本会附带节点作用域工具清单，要求模型按步骤使用指定节点工具

### 3.4 Communication Layer — MQTT 消息总线

选择 MQTT 作为通信层的理由：
- **会话持久**：QoS 2 保证消息不丢失，即使节点临时离线
- **低带宽**：适合 iPhone 蜂窝网络
- **双向**：支持 pub/sub 和 request/reply
- **WebSocket 支持**：iPhone/MacBook 可通过 WebSocket 连接
- **成熟生态**：Mosquitto/EMQX 作为 Broker

```python
# nexus/mesh/transport.py

class MeshTransport:
    """
    基于 MQTT 的节点间通信层。

    Topic 设计:
      nexus/nodes/{node_id}/heartbeat     — 心跳
      nexus/nodes/{node_id}/capabilities  — 能力注册/变更
      nexus/tasks/{task_id}/assign        — 任务分配
      nexus/tasks/{task_id}/status        — 任务状态更新
      nexus/tasks/{task_id}/result        — 任务结果
      nexus/tasks/{task_id}/artifacts     — 任务产物传输
      nexus/broadcast/capability_update   — 全局能力变更通知
      nexus/broadcast/evolution           — 进化事件通知
      nexus/rpc/{node_id}/{request_id}    — RPC 请求/响应
    """

    async def publish_task_assignment(
        self,
        node_id: str,
        task_step: TaskStep,
        context: dict,
    ) -> None:
        """向目标节点发送任务分配"""

    async def request_tool_execution(
        self,
        node_id: str,
        tool_name: str,
        arguments: dict,
        timeout: float = 30.0,
    ) -> ToolResult:
        """
        远程工具调用 (RPC over MQTT)。

        当本地 Agent 需要调用远程节点的工具时使用。
        例如：Ubuntu 上的 Agent 需要调用 MacBook 的 browser_navigate。
        """

    async def stream_artifact(
        self,
        target_node: str,
        artifact_type: str,
        data: bytes,
        metadata: dict,
    ) -> str:
        """传输大型产物（文件、音频、截图等）"""
```

### 3.5 Remote Tool Proxy — 远程工具代理

让每个节点可以像调用本地工具一样调用远程节点的工具。

```python
# nexus/mesh/remote_tools.py

class RemoteToolProxy:
    """
    将远程节点的工具注册为本地 ToolDefinition。

    当 Mesh Registry 中某个能力只在远程节点上可用时，
    自动创建一个代理工具，通过 MQTT RPC 调用远程节点执行。
    """

    def __init__(
        self,
        transport: MeshTransport,
        registry: MeshRegistry,
        local_node_id: str,
    ):
        self._transport = transport
        self._registry = registry
        self._local_node_id = local_node_id

    def build_remote_tools(self) -> list[ToolDefinition]:
        """
        扫描 Mesh Registry，为所有非本地的能力创建代理工具。

        例如，Ubuntu 节点会创建：
        - mesh__bWFjYm9vay1wcm8__browser_navigate → 代理到 MacBook
        - mesh__aXBob25l__take_photo → 代理到 iPhone
        - mesh__aXBob25l__push_notify → 代理到 iPhone
        """
        remote_tools = []
        for node in self._registry.list_nodes(online_only=True):
            if node.node_id == self._local_node_id:
                continue
            for cap in node.capabilities:
                tool_specs = cap.properties.get("tool_specs", {})
                for tool_name in cap.tools:
                    spec = tool_specs.get(tool_name, {})
                    alias = self.alias_for(node.node_id, tool_name)
                    remote_tools.append(
                        ToolDefinition(
                            name=alias,
                            description=f"[Remote: {node.display_name}] {spec.get('description', cap.description)}",
                            parameters=spec.get("parameters", {"type": "object", "properties": {}}),
                            handler=self._make_remote_handler(alias),
                            risk_level=ToolRiskLevel(spec.get("risk_level", "medium")),
                            tags=["mesh", "remote", *spec.get("tags", [])],
                        )
                    )
        return remote_tools

    def _make_remote_handler(self, aliased_tool_name: str):
        async def handler(**kwargs):
            node_id, tool_name = self.parse_alias(aliased_tool_name)
            result = await self._transport.request(
                target_node=node_id,
                payload={"tool_name": tool_name, "arguments": kwargs},
            )
            if not result.payload.get("success"):
                raise RuntimeError(result.payload.get("error") or f"Remote tool {tool_name}@{node_id} failed")
            return result.payload.get("output", "")
        return handler
```

关键策略：
- 远端工具必须带节点作用域别名，不能与 Hub 本地同名工具共用同一个名字
- `browser_navigate` 这类同名工具如果在 Hub 和 Mac 都存在，本轮 run 只能保留一个来源
- 当步骤分配给 Mac 时，Hub 只注入 `mesh__...__browser_navigate`，并禁用 Hub 本地 `browser_navigate`
- 当步骤分配给 Ubuntu 时，则只保留 Ubuntu 本地工具，不注入远端同名工具

### 3.6 Local LLM 作为特殊能力

本地 LLM（Ubuntu 上的 Ollama）不是全局默认 Provider，而是一个**可被发现和调度的特殊能力**。

```yaml
# 在 Ubuntu Node Card 中注册为 capability
capabilities:
  - id: "local_llm_inference"
    description: "本地 LLM 推理，数据不出本地网络，适合敏感数据处理"
    tools: ["local_llm_generate", "local_llm_chat", "local_llm_embed"]
    properties:
      privacy: "local_only"           # 数据不出本地
      models:
        - name: "qwen2.5:72b"
          context_length: 32768
          speed: "~30 tok/s"
        - name: "llama3.1:8b"
          context_length: 128000
          speed: "~120 tok/s"
      gpu: "Server GPU"
      concurrent_requests: 2
```

Task Router 在规划时会考虑：
- **隐私敏感任务**（如分析个人健康数据）→ 优先路由到 `local_llm_inference`
- **需要大 context 的任务**（如长文档分析）→ 可选本地 LLM
- **离线时的降级**：MacBook 离线时，如果有 Ollama 可用，可在本地跑小模型

```python
# nexus/mesh/task_router.py — 路由策略扩展

class RoutingPolicy:
    """定义任务到节点+LLM的路由策略"""

    def select_provider(
        self,
        task_step: TaskStep,
        available_providers: list[ProviderInfo],
    ) -> ProviderInfo:
        """
        选择 LLM Provider 的策略：

        1. 如果任务标记 privacy=local_only
           → 必须用本地 LLM (Ollama)
        2. 如果任务需要超长 context (>64K tokens)
           → 优先 Kimi (128K) > Qwen > 本地 LLM
        3. 如果任务是代码生成/结构化输出
           → 优先 Qwen > Kimi
        4. 如果任务是中文长文写作
           → 优先 Kimi > Qwen
        5. 默认
           → 按 Provider Gateway 的健康状态和延迟选择
        """
```

---

## 4. 任务生命周期（跨节点）

以用户描述的场景为例：**"打开浏览器抓取内容 → 长时间分析 → 移动端展示结果 → 接收新输入"**

```
User (via Feishu/Web)
  │
  ▼
Hub (Ubuntu) — Orchestrator
  │
  ├─ Step 1: Plan Task
  │   LLM 分解为 4 个步骤，查询 Mesh Registry
  │
  ├─ Step 2: Assign → MacBook (browser_automation)
  │   │  MQTT: nexus/tasks/t001/assign → MacBook
  │   │
  │   ▼
  │  MacBook Agent Runtime:
  │   - 收到任务分配
  │   - 用自己的 LLM API (Kimi) 理解具体操作
  │   - 执行 browser_navigate → 需要登录
  │   - 推送通知给用户："请在浏览器中完成登录"
  │   - 用户登录后继续
  │   - browser_extract_text → 抓取内容
  │   - MQTT: nexus/tasks/t001/result → Hub
  │   - MQTT: nexus/tasks/t001/artifacts → 传输抓取的内容
  │
  ├─ Step 3: Assign → Ubuntu (long_running_analysis)
  │   │  收到 Step 2 的结果
  │   │
  │   ▼
  │  Ubuntu Agent Runtime:
  │   - 收到抓取的内容
  │   - 用自己的 LLM API (Qwen) 或本地 LLM 分析
  │   - 长时间运行（MacBook 可以合盖，不影响）
  │   - 分析完成，结果存入 Vault
  │   - MQTT: nexus/tasks/t001/result → Hub
  │
  ├─ Step 4: Assign → iPhone (result_display + input_collection)
  │   │  MQTT: nexus/tasks/t001/assign → iPhone
  │   │
  │   ▼
  │  iPhone Agent Runtime:
  │   - 推送通知："分析结果已准备好"
  │   - 用户点开，展示分析报告
  │   - 用户拍照/录音补充信息
  │   - MQTT: nexus/tasks/t001/artifacts → Hub
  │
  └─ Step 5: Assign → Ubuntu (integrate_input)
       - 整合 iPhone 的新输入
       - 更新分析结果
       - 存入 Vault
       - 通知用户完成
```

### 4.1 任务状态机（跨节点扩展）

```
                    ┌──────────────────────────┐
                    │                          │
    ┌───────┐    ┌──▼──────┐    ┌──────────┐   │
    │created├───►│planning ├───►│assigning │   │
    └───────┘    └─────────┘    └────┬─────┘   │
                                     │         │
                        ┌────────────┤         │
                        │            │         │
                 ┌──────▼──┐   ┌────▼─────┐   │
                 │waiting  │   │dispatched│   │
                 │_for_node│   └────┬─────┘   │
                 └────┬────┘        │         │
                      │       ┌────▼─────┐   │
                      │       │ running   │   │
                      │       │ @node_x   │   │
                      │       └──┬──┬─────┘   │
                      │          │  │         │
                      │     ┌────┘  └────┐    │
                      │     │            │    │
                 ┌────▼─────▼┐    ┌─────▼──┐ │
                 │ succeeded │    │ failed  ├─┘
                 └───────────┘    └────────┘
                                  (retry/reroute)
```

新增状态：
- `assigning`: 正在匹配最佳节点
- `dispatched`: 已发送到目标节点，等待确认
- `waiting_for_node`: 所需节点离线，等待上线
- `running @node_x`: 在特定节点上执行中

---

## 5. 跨节点自我学习与进化

### 5.1 问题定义

当需要一个新能力，且该能力涉及多个节点时，系统需要：
1. **分析**：这个能力需要哪些平台的什么资源
2. **拆分**：将能力拆解为各平台上的组件
3. **部署**：在各节点上安装/配置
4. **协调**：定义组件间的交互协议
5. **验证**：端到端测试
6. **注册**：更新 Mesh Registry

### 5.2 Cross-Node Evolution Runtime

```python
# nexus/mesh/evolution.py

@dataclass
class CrossNodeCapabilityPlan:
    """跨节点能力创建计划"""
    capability_name: str
    description: str
    components: list[NodeComponent]
    interaction_protocol: InteractionProtocol
    verification_steps: list[VerificationStep]

@dataclass
class NodeComponent:
    """单个节点上需要部署的组件"""
    node_id: str
    platform: str
    component_type: str           # skill | tool | service | config
    spec: dict                    # 部署规格
    dependencies: list[str]       # 依赖的其他组件
    install_steps: list[str]      # 安装命令

@dataclass
class InteractionProtocol:
    """组件间交互协议"""
    trigger: str                  # 触发方式
    data_flow: list[DataFlowStep] # 数据流转
    error_handling: str           # 错误处理策略

class CrossNodeEvolutionManager:
    """
    跨节点能力进化管理器。

    工作流程：
    1. 用户/系统发现能力缺口
    2. LLM 分析能力需求，拆解为各节点组件
    3. 在各节点的 Sandbox 中验证
    4. 逐节点部署
    5. 端到端验证
    6. 注册到 Mesh Registry
    7. 记录审计日志
    """

    async def plan_capability(
        self,
        capability_description: str,
        context: str = "",
    ) -> CrossNodeCapabilityPlan:
        """
        用 LLM 规划跨节点能力。

        输入：自然语言描述的能力需求
        输出：跨节点部署计划

        LLM Prompt 包含：
        1. 当前 Mesh 中各节点的 Node Card
        2. 各节点已有的能力和工具
        3. 各节点的平台特性（macOS API、Linux 服务、iOS 框架）
        4. 历史成功的能力部署记录
        """
        mesh_context = await self._build_evolution_context()

        plan = await self._provider.generate(
            prompt=f"""
分析以下能力需求，设计跨节点部署方案：

需求：{capability_description}
{f'补充上下文：{context}' if context else ''}

当前网络状态：
{mesh_context}

请输出：
1. 这个能力需要在哪些节点上部署什么组件
2. 每个组件的技术实现方案（具体到工具/脚本/服务）
3. 组件之间的数据流转和交互协议
4. 验证方案
5. 回滚策略
""",
        )
        return self._parse_plan(plan)

    async def deploy_capability(
        self,
        plan: CrossNodeCapabilityPlan,
        *,
        dry_run: bool = False,
    ) -> DeploymentResult:
        """
        执行跨节点部署。

        策略：
        1. 先在各节点的 Sandbox 中验证组件
        2. 按依赖顺序逐节点部署
        3. 部署后立即进行单节点验证
        4. 全部部署完成后进行端到端验证
        5. 任何步骤失败，已部署的组件全部回滚
        """
        results = []

        # 按依赖顺序排序
        ordered = self._topological_sort(plan.components)

        for component in ordered:
            if dry_run:
                # 只在 Sandbox 中验证
                result = await self._sandbox_verify(component)
            else:
                # 实际部署
                result = await self._deploy_component(component)

            results.append(result)
            if not result.success:
                # 回滚已部署的组件
                await self._rollback_deployed(results)
                return DeploymentResult(
                    success=False,
                    reason=f"Component {component.node_id}:{component.component_type} failed",
                    results=results,
                )

        # 端到端验证
        e2e_result = await self._e2e_verify(plan)
        if not e2e_result.passed:
            await self._rollback_deployed(results)
            return DeploymentResult(
                success=False,
                reason=f"E2E verification failed: {e2e_result.summary}",
                results=results,
            )

        # 注册到 Mesh Registry
        await self._register_capability(plan)

        return DeploymentResult(success=True, results=results)

    async def _deploy_component(self, component: NodeComponent) -> ComponentResult:
        """
        在目标节点上部署组件。

        通过 MQTT RPC 调用目标节点的 Evolution Runtime：
        1. 发送组件规格
        2. 目标节点在本地 Sandbox 中验证
        3. 验证通过后安装
        4. 返回结果
        """
        return await self._transport.request_tool_execution(
            node_id=component.node_id,
            tool_name="evolution_deploy_component",
            arguments={
                "component_type": component.component_type,
                "spec": component.spec,
                "install_steps": component.install_steps,
            },
        )
```

### 5.3 自我学习：能力缺口自动检测

```python
# nexus/mesh/learning.py

class CapabilityLearner:
    """
    从用户交互中学习能力缺口，自动提出进化建议。

    信号来源：
    1. 任务失败记录（"没有节点能处理这个请求"）
    2. 用户频繁使用的模式（"每次都要手动做 X"）
    3. 任务路由效率（"这个任务本可以更快完成"）
    4. 跨节点协作的摩擦点
    """

    async def analyze_gaps(self) -> list[CapabilityGap]:
        """
        分析最近 N 天的任务日志，识别能力缺口。

        返回建议列表，每个建议包含：
        - 描述：缺什么能力
        - 频率：被需要的频率
        - 影响：缺少这个能力的影响
        - 建议方案：如何实现
        - 涉及节点：需要在哪些节点上部署
        """

    async def propose_optimization(self) -> list[OptimizationProposal]:
        """
        分析任务执行效率，提出优化建议。

        例如：
        - "浏览器抓取 + 分析"这个模式出现了 15 次，
          建议创建一个专用 Skill 自动化这个流程
        - MacBook 上的音频转录延迟很高，
          建议在 Ubuntu 上用 GPU 加速的 SenseVoice 处理
        """

    async def learn_from_success(self, task_plan: TaskPlan) -> None:
        """
        从成功的任务执行中学习：
        1. 记录有效的跨节点协作模式
        2. 更新路由策略的权重
        3. 生成可复用的 Workflow 模板
        """
```

### 5.4 Evolution Audit — 跨节点审计

```python
# 扩展现有的 AuditLog

class MeshAuditLog(AuditLog):
    """
    扩展审计日志，支持跨节点进化事件。

    新增事件类型：
    - cross_node_capability_planned
    - component_deployed@{node_id}
    - component_verified@{node_id}
    - component_rolled_back@{node_id}
    - cross_node_capability_activated
    - cross_node_capability_failed
    - routing_policy_updated
    - capability_gap_detected
    """
```

---

## 6. 各节点的具体实现

### 6.1 Ubuntu Server (Hub)

在现有 Nexus 基础上增加：

```
nexus/
  mesh/
    __init__.py
    registry.py          # MeshRegistry
    task_router.py       # TaskRouter
    transport.py         # MeshTransport (MQTT)
    remote_tools.py      # RemoteToolProxy
    evolution.py         # CrossNodeEvolutionManager
    learning.py          # CapabilityLearner
    node_card.py         # NodeCard 数据结构
    state_sync.py        # 跨节点状态同步
    protocol.py          # 消息协议定义
  ...existing modules...
```

配置增加：
```yaml
# config/app.yaml 新增
mesh:
  enabled: true
  node_id: "ubuntu-server-hub"
  node_type: "hub"
  mqtt:
    broker_host: "0.0.0.0"
    broker_port: 1883
    websocket_port: 8883
    tls: true
    cert_path: "./certs/server.crt"
    key_path: "./certs/server.key"
  registry:
    heartbeat_interval_seconds: 30
    node_timeout_seconds: 90
    capability_cache_ttl_seconds: 300
  routing:
    prefer_local: true
    max_remote_timeout_seconds: 60
    privacy_policy: "prefer_local_llm"
```

### 6.2 MacBook Pro (Edge Node)

轻量级 Agent Runtime，专注本地能力暴露：

```
nexus/
  edge/
    agent.py            # 节点 Agent（接收任务、执行、回报）
    tools.py            # browser/filesystem/screen/clipboard 工具宿主
  mesh/
    transport.py        # MQTT / InMemory 传输层
    remote_tools.py     # 远端工具代理
    task_protocol.py    # 结构化任务分配协议
  config/
    node_cards/
      macbook-pro.example.yaml
```

关键设计：
- **启动时**：连接 MQTT Broker，注册 Node Card
- **运行中**：监听任务分配，执行 Hub 下发的结构化步骤
- **合盖时**：发送 offline 心跳，Hub 将依赖该节点的任务标记为 waiting_for_node
- **开盖时**：重新注册，恢复被阻塞的任务

```python
# nexus/edge/agent.py

class EdgeNodeAgent:
    """MacBook 上的节点 Agent"""

    async def start(self):
        """启动节点"""
        # 1. 连接 MQTT Broker
        await self._transport.connect()

        # 2. 注册 Node Card
        node_card = self._load_node_card()
        await self._transport.publish(
            f"nexus/nodes/{self._node_id}/card",
            self._transport.make_message(
                MessageType.NODE_REGISTER,
                f"nexus/nodes/{self._node_id}/card",
                node_card.to_dict(),
            ),
        )

        # 3. 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 4. 监听任务分配
        await self._transport.subscribe(
            f"nexus/tasks/+/assign",
            self._on_task_assigned,
        )

        # 5. 监听远程工具调用
        await self._transport.subscribe(
            f"nexus/rpc/{self._node_id}/+",
            self._on_rpc_request,
        )

    async def _on_task_assigned(self, topic: str, message: MeshMessage):
        """收到任务分配"""
        task_step = TaskStep.from_dict(message.payload)

        # 执行 Hub 下发的结构化 tool calls；本地 LLM 只做受限的节点内推理
        result = await self._local_runtime.execute_step(
            execution_payload=task_step.execution_payload,
            tools=self._local_tools,
            provider=self._provider_gateway,
        )

        # 回报结果
        await self._transport.publish(
            f"nexus/tasks/{task_step.task_id}/result",
            self._transport.make_message(
                MessageType.TASK_RESULT,
                f"nexus/tasks/{task_step.task_id}/result",
                {
                    "step_id": task_step.step_id,
                    "node_id": self._node_id,
                    "result": result,
                },
            ),
        )

    async def _on_rpc_request(self, topic: str, message: MeshMessage):
        """处理远程工具调用"""
        tool_name = message.payload["tool_name"]
        arguments = message.payload["arguments"]
        request_id = topic.split("/")[-1]

        tool = self._tool_registry.get(tool_name)
        if not tool:
            result = {"success": False, "error": f"Unknown tool: {tool_name}"}
        else:
            try:
                output = await tool.handler(**arguments)
                result = {"success": True, "output": str(output)}
            except Exception as e:
                result = {"success": False, "error": str(e)}

        await self._transport.publish(
            f"nexus/rpc/{self._node_id}/{request_id}/response",
            self._transport.make_message(
                MessageType.RPC_RESPONSE,
                f"nexus/rpc/{self._node_id}/{request_id}/response",
                {**result, "request_id": request_id},
                target_node=message.source_node,
            ),
        )
```

### 6.3 iPhone (Mobile Node)

最轻量的实现，用 Swift/SwiftUI：

```
NexusMobile/
  Core/
    MeshClient.swift         # MQTT 客户端
    NodeAgent.swift          # 节点 Agent
    TaskExecutor.swift       # 任务执行器
    LLMProvider.swift        # API LLM 调用
  Capabilities/
    CameraCapability.swift   # 拍照/录像
    AudioCapability.swift    # 录音
    LocationCapability.swift # 定位
    NotificationCapability.swift  # 推送
    HealthCapability.swift   # HealthKit
    DisplayCapability.swift  # 结果展示
  UI/
    ChatView.swift           # 聊天界面
    TaskView.swift           # 任务状态
    ResultView.swift         # 结果展示
    InputView.swift          # 拍照/录音输入
```

关键设计：
- **联网能力存在，但后台长连不作为架构前提**：前台可使用 WebSocket / MQTT over WebSocket，后台依赖 APNs / Feishu 触发
- **推送通知集成**：用于唤起用户、进入前台流程、做结果确认
- **能力暴露**：摄像头、麦克风、GPS、HealthKit 等 iOS 原生能力
- **轻量 LLM 调用**：只用 API LLM 处理简单的本地理解任务
- **结果展示**：Rich UI 展示分析结果（图表、报告、卡片）

当前阶段建议：
- `Feishu` 先作为移动端主界面和通知入口
- `iPhone App` 作为后续增强项，仅在需要更深 iOS 原生能力时再引入

---

## 7. 数据流与状态同步

### 7.1 知识同步策略

```
Ubuntu (Source of Truth)
  │
  ├─► MacBook (按需同步)
  │   - 同步当前任务相关的 Vault 文档
  │   - 同步用户 Memory (USER.md, SOUL.md)
  │   - 不同步全量知识库（太大）
  │
  └─► iPhone (极少同步)
      - 只同步需要展示的结果
      - 不存储知识库
```

### 7.2 产物传输

```python
# 大文件传输策略
class ArtifactTransfer:
    """
    跨节点产物传输。

    策略：
    1. 小文件 (<1MB)：直接通过 MQTT 消息体
    2. 中文件 (1-50MB)：分片通过 MQTT
    3. 大文件 (>50MB)：通过 HTTP 直传（同一局域网）
                       或 S3/MinIO 中转（跨网络）
    """
```

---

## 8. 安全模型

### 8.1 节点认证

```yaml
# 每个节点有一对密钥
security:
  node_id: "macbook-pro-user"
  private_key: "./certs/node.key"
  certificate: "./certs/node.crt"
  hub_ca: "./certs/hub-ca.crt"
  # MQTT 连接使用 TLS + 客户端证书认证
```

### 8.2 工具调用权限

```python
class MeshToolsPolicy:
    """
    跨节点工具调用权限控制。

    规则：
    1. Hub 可以调用任何节点的工具
    2. Edge 节点之间需要 Hub 授权
    3. Mobile 节点只能被调用，不能主动调用其他节点
    4. 敏感工具（文件写入、系统命令）需要用户确认
    5. 隐私数据（健康、位置）需要明确授权
    """
```

---

## 9. 差距分析与架构修正（2026-03-15 更新）

基于实际实现进展和使用反馈，识别出以下关键差距并提出修正方案。

### 9.1 差距一：MacBook 是"工具执行器"而非"Agent Runtime"

**现状**：`EdgeNodeAgent` 只处理 RPC 工具调用和结构化 `TaskAssignment`。用户输入全部通过 WebSocket 透传到 Hub，MacBook 没有自己的 Agent tool-calling loop，也没有 `ProviderGateway`。

**问题**：
- MacBook 离线时完全不能工作，违背"离线可降级独立工作"的原则
- MacBook 本地的 LLM API 接入能力未被利用
- 复杂的多步本地任务（如"打开浏览器 → 等用户登录 → 抓取 → 整理"）需要在本地完成 tool-calling loop，不能每步都 round-trip 回 Hub

**修正方案：Edge Agent Runtime 升级**

```
                       ┌─────────────────────────────────────┐
                       │      MacBook Edge Node              │
                       │                                     │
  用户输入 ──►         │  ┌─────────────┐  ┌──────────────┐  │
  (本地 UI)            │  │ Local Agent  │  │ Provider     │  │
                       │  │ Runtime      │  │ Gateway      │  │
  Hub 下发 ──►         │  │              │  │              │  │
  (MQTT task)          │  │ - tool loop  │  │ - Kimi API   │  │
                       │  │ - session    │  │ - Qwen API   │  │
                       │  │ - streaming  │  │ - fallback   │  │
                       │  └──────┬───────┘  └──────────────┘  │
                       │         │                             │
                       │  ┌──────▼───────┐  ┌──────────────┐  │
                       │  │ Local Tool   │  │ Remote Tool  │  │
                       │  │ Registry     │  │ Proxy        │  │
                       │  │              │  │              │  │
                       │  │ - browser    │  │ - vault →Hub │  │
                       │  │ - screen     │  │ - llm →Hub   │  │
                       │  │ - clipboard  │  │ - audio→Hub  │  │
                       │  │ - shortcuts  │  │              │  │
                       │  └──────────────┘  └──────────────┘  │
                       └─────────────────────────────────────┘
```

关键变更：
1. MacBook sidecar 增加 `ProviderGateway` 实例（配置 Kimi/Qwen API key）
2. MacBook sidecar 增加轻量 `AgentRuntime`，能独立执行 tool-calling loop
3. 两种执行模式：
   - **Hub 委托模式**（默认）：Hub 分解任务，下发结构化 `TaskAssignment`，MacBook 按指令执行
   - **本地自主模式**（离线/Hub 不可达时）：MacBook 用本地 ProviderGateway 驱动自己的 Agent loop，只使用本地工具

```python
# nexus/edge/agent.py — 扩展

class EdgeNodeAgent:
    async def _on_task_assigned(self, topic, message):
        assignment = TaskAssignment.from_dict(message.payload)

        if assignment.metadata.get("execution_mode") == "agent_loop":
            # Hub 要求 MacBook 自主执行多步任务
            result = await self._local_agent_runtime.run(
                task=assignment.metadata["task_description"],
                tools=self._local_tools,
                constraints=assignment.metadata.get("constraints", {}),
            )
        else:
            # 结构化单工具调用（当前模式）
            result = await self._execute_tool(assignment.tool_name, assignment.arguments)

    async def handle_local_command(self, user_input: str):
        """用户在本地 UI 直接输入，不经过 Hub"""
        if self._hub_reachable():
            # Hub 在线：转发给 Hub 规划，等 Hub 分配回来
            await self._forward_to_hub(user_input)
        else:
            # Hub 离线：本地 Agent loop 执行
            result = await self._local_agent_runtime.run(
                task=user_input,
                tools=self._local_tools,  # 只有本地工具
            )
            await self._display_result(result)
```

### 9.2 差距二：本地 LLM 未作为可调度的 Mesh 能力

**现状**：`ubuntu-server-hub.yaml` 声明了 `local_llm_inference` capability 和 `local_llm_generate` tool，但没有对应的 tool handler 实现。Ollama 在 Ubuntu 上运行但未接入 Mesh。

**修正方案：Local LLM Service + Mesh 注册**

```python
# nexus/services/local_llm.py

class LocalLLMService:
    """
    将本地 Ollama 作为 Mesh 可调度能力暴露。

    与 ProviderGateway 的区别：
    - ProviderGateway 是 Agent Runtime 内部使用的 LLM 接口
    - LocalLLMService 是作为 Mesh Tool 暴露给其他节点调用的

    使用场景：
    1. 隐私敏感数据处理（数据不出本地网络）
    2. 大量文本的批量处理（利用本地 GPU 无 API 费用）
    3. Embedding 生成（本地向量化）
    4. 其他节点需要 LLM 但想避免 API 调用时
    """

    def __init__(self, ollama_base_url: str = "http://127.0.0.1:11434"):
        self._base_url = ollama_base_url

    async def generate(
        self,
        prompt: str,
        *,
        model: str = "qwen2.5:72b",
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """单次生成"""

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str = "qwen2.5:72b",
        temperature: float = 0.7,
    ) -> str:
        """多轮对话"""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str = "nomic-embed-text",
    ) -> list[list[float]]:
        """批量向量化"""

    async def health(self) -> dict:
        """检查 Ollama 状态和已加载模型"""

    def build_tools(self) -> list[ToolDefinition]:
        """生成可注册到 Mesh 的工具定义"""
        return [
            ToolDefinition(
                name="local_llm_generate",
                description="使用本地 GPU LLM 生成文本（数据不出本地）",
                parameters={...},
                handler=self._handle_generate,
                risk_level=ToolRiskLevel.LOW,
                tags=["llm", "local", "privacy"],
            ),
            ToolDefinition(
                name="local_llm_embed",
                description="使用本地模型生成文本向量",
                parameters={...},
                handler=self._handle_embed,
                risk_level=ToolRiskLevel.LOW,
                tags=["llm", "local", "embedding"],
            ),
        ]
```

RoutingPolicy 的隐私感知路由已在 `task_router.py` 中实现（`privacy_local_only` metadata），但需要确保：
- 当 `local_llm_inference` 能力在线时，TaskRouter 能正确发现和路由
- 当任务包含隐私关键词时，自动标记 `privacy_local_only=True`

### 9.3 差距三：跨节点进化缺少执行闭环

**现状**：设计中描述了 `CrossNodeEvolutionManager` 和 `CapabilityLearner`，但完全未实现。

**核心问题**：如何从"LLM 生成一个计划"到"在 MacBook + Ubuntu 上都真正部署代码/配置并验证"？

**修正方案：分层进化执行引擎**

```
能力需求
  │
  ▼
┌──────────────────────────────────────────────────────┐
│ 1. CapabilityPlanner（Hub LLM）                       │
│    分析需求 → 拆解为 NodeComponent[]                   │
│    输入：Mesh Registry 能力目录 + 平台特性知识          │
│    输出：CrossNodeCapabilityPlan                      │
└─────────────────────┬────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────┐
│ 2. ComponentGenerator（Hub LLM + Evolution Runtime）   │
│    为每个 NodeComponent 生成具体代码/配置               │
│    - Python skill → 生成 .py 文件                     │
│    - macOS tool → 生成工具定义 + handler               │
│    - Config → 生成 YAML 配置变更                       │
│    - iOS capability → 生成 Swift 代码模板              │
└─────────────────────┬────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────┐
│ 3. DistributedDeployer（MQTT RPC）                     │
│    按拓扑排序逐节点部署：                               │
│    - Hub: 本地 Sandbox 验证 → install                  │
│    - MacBook: RPC 调用 evolution_deploy_component      │
│    - iPhone: 推送通知 + 用户手动更新（Phase 3+）        │
│    每步失败 → 回滚已部署节点                            │
└─────────────────────┬────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────┐
│ 4. E2E Verifier                                       │
│    端到端验证：Hub 发起测试任务 → 跨节点执行 → 断言结果 │
│    失败 → 全量回滚                                     │
│    成功 → 注册新能力到 MeshRegistry + 广播通知          │
└──────────────────────────────────────────────────────┘
```

MacBook 侧需要增加 `evolution_deploy_component` tool：

```python
# nexus/edge/tools.py — 新增

async def evolution_deploy_component(
    component_type: str,
    spec: dict,
    install_steps: list[str],
) -> str:
    """
    在 MacBook 本地部署一个能力组件。

    component_type:
      - "tool": 注册新工具到 EdgeToolExecutor
      - "config": 更新本地配置
      - "script": 在 Sandbox 中执行安装脚本

    安全约束：
      - 所有部署必须在 Sandbox 中先验证
      - 需要用户审批（requires_approval=True）
      - 不允许修改系统关键路径
    """
```

### 9.4 差距四：节点能力的动态学习机制

这是一个新增的架构关注点，原设计中 `CapabilityLearner` 只做了概要描述。

**分三个层次实现学习：**

**层次 1：被动记录（立即可做）**

```python
class TaskJournal:
    """记录每次任务执行的路由决策和结果"""

    async def record(
        self,
        plan: TaskPlan,
        results: list[TaskExecutionResult],
        user_feedback: str | None = None,
    ):
        """
        记录到 vault/_system/mesh/task_journal/
        每条记录包含：
        - 原始任务描述
        - 规划出的步骤和节点分配
        - 每步的执行结果（成功/失败/耗时）
        - 用户反馈（如果有）
        - 路由决策的原因
        """
```

**层次 2：模式识别（定期分析）**

```python
class PatternAnalyzer:
    """定期分析 TaskJournal，识别模式"""

    async def analyze(self) -> list[Insight]:
        """
        发现模式如：
        - "浏览器抓取 → 分析整理" 这个模式出现了 15 次
          → 建议创建 combined skill
        - MacBook 上 capture_screen 的平均延迟 2.3s
          → 正常，不需要优化
        - 某个自定义 Skill 在 Ubuntu 上失败了 3 次
          → 建议检查依赖
        - 用户 60% 的任务只用了 Hub 本地工具
          → 当前 Mesh 利用率偏低
        """
```

**层次 3：主动进化（能力缺口检测 + 自动提案）**

```python
class CapabilityGapDetector:
    """从任务失败和 waiting_for_node 事件中检测能力缺口"""

    async def detect(self) -> list[CapabilityGap]:
        """
        信号来源：
        1. TaskRouter 返回 waiting_for_node（所需能力无节点提供）
        2. 工具调用失败且 error 包含"Unknown tool"
        3. 用户反馈"我需要 xxx 功能"
        4. 任务被路由到次优节点（如长任务被分配到 MacBook）

        输出：
        - gap_id: 唯一标识
        - description: 缺少什么能力
        - frequency: 最近 N 天被需要的次数
        - involved_platforms: 涉及哪些节点/平台
        - suggested_plan: LLM 生成的实现建议（可选）
        - priority: 基于频率和影响的优先级
        """
```

### 9.5 修正后的节点角色矩阵

```
┌──────────────┬─────────────────────┬────────────────────┬──────────────────┐
│              │ Ubuntu Hub          │ MacBook Edge       │ iPhone Mobile    │
├──────────────┼─────────────────────┼────────────────────┼──────────────────┤
│ Agent        │ 完整 Agent Runtime  │ 完整 Agent Runtime │ 轻量 Agent       │
│ Runtime      │ + TaskRouter        │ + 本地 tool loop   │ (前台时)         │
│              │ + Orchestrator      │ + Hub 委托模式     │                  │
├──────────────┼─────────────────────┼────────────────────┼──────────────────┤
│ LLM 接入     │ Kimi API           │ Kimi API           │ Kimi API         │
│              │ Qwen API           │ Qwen API           │ (轻量调用)       │
│              │ Ollama (本地)       │                    │                  │
├──────────────┼─────────────────────┼────────────────────┼──────────────────┤
│ 特有能力     │ knowledge_store     │ browser_automation │ camera_photo     │
│              │ local_llm_inference │ local_filesystem   │ camera_video     │
│              │ audio_transcription │ screen_capture     │ microphone       │
│              │ long_running_analy. │ clipboard          │ location         │
│              │ document_management │ apple_shortcuts    │ push_notification│
│              │ evolution_runtime   │ audio_recording    │ health_data      │
│              │                     │ video_capture      │ result_display   │
│              │                     │ run_applescript    │ contacts         │
├──────────────┼─────────────────────┼────────────────────┼──────────────────┤
│ 在线模式     │ 7x24 永久在线       │ 间歇在线           │ 前台/推送唤起    │
│              │ MQTT Broker         │ MQTT Client        │ MQTT/WebSocket   │
├──────────────┼─────────────────────┼────────────────────┼──────────────────┤
│ 离线行为     │ 不会离线            │ 降级为本地 Agent   │ 推送通知排队     │
│              │                     │ 只用本地工具+API   │                  │
├──────────────┼─────────────────────┼────────────────────┼──────────────────┤
│ 进化角色     │ 规划者+部署者       │ 执行者（接收部署） │ 用户手动更新     │
│              │ CapabilityLearner   │ evolution_deploy   │                  │
│              │ CrossNodeEvolution  │ _component tool    │                  │
└──────────────┴─────────────────────┴────────────────────┴──────────────────┘
```

### 9.6 修正后的数据流：MacBook 本地自主模式

```
MacBook (Hub 不可达)

  用户输入: "整理一下桌面上的截图"
    │
    ▼
  Local Agent Runtime
    │
    ├─ 检查 Hub → 不可达
    ├─ 降级为本地模式
    ├─ 调用本地 ProviderGateway (Kimi API)
    │   System: "你是 MacBook 上的本地助手。可用工具: list_local_files, code_read_file,
    │            capture_screen, read_clipboard, write_clipboard, run_applescript..."
    │
    ├─ Tool loop:
    │   1. list_local_files(path="~/Desktop", pattern="*.png")
    │   2. 分析文件名和时间戳
    │   3. run_applescript("创建文件夹并移动文件")
    │
    ├─ 生成结果
    │
    └─ Hub 恢复后，将任务日志同步到 Hub TaskJournal
```

---

## 10. 实施路线（修正版）

### Phase 0: 基础通信 ✅ 已完成
- [x] MQTT Broker、MeshTransport、NodeCard、MeshRegistry、集成测试

### Phase 1: MacBook Edge Agent ✅ 已完成
- [x] EdgeNodeAgent、macOS 工具集、RemoteToolProxy、SwiftUI 菜单栏应用

### Phase 2: 能力感知路由 ✅ 已完成
- [x] TaskRouter (LLM + 启发式)、RoutingPolicy、离线重路由

### Phase 2.5: Edge Agent Runtime 升级 ⬅ **当前优先**
- [ ] MacBook sidecar 增加 `ProviderGateway` 实例（Kimi/Qwen API key 配置）
- [ ] MacBook sidecar 增加轻量 `AgentRuntime`（复用 `nexus.agent.core` 的 tool loop）
- [ ] 实现双模式执行：Hub 委托模式 + 本地自主模式
- [ ] Hub 不可达时自动降级为本地模式
- [ ] MacBook 本地 UI 支持直接输入（不经过 Hub）
- [ ] Hub 恢复后同步任务日志到 TaskJournal
- [ ] 测试：Hub 离线时 MacBook 独立执行本地任务

### Phase 3: 本地 LLM 集成
- [ ] 实现 `LocalLLMService`（Ollama wrapper）
- [ ] 注册 `local_llm_generate` / `local_llm_embed` 为 Mesh 工具
- [ ] Ollama 健康检查 + 模型加载状态上报
- [ ] RoutingPolicy 隐私感知路由验证
- [ ] 测试：MacBook 通过 RemoteToolProxy 调用 Ubuntu 本地 LLM

### Phase 4: 任务日志与学习
- [ ] 实现 `TaskJournal`（记录路由决策和执行结果）
- [ ] 实现 `PatternAnalyzer`（定期模式识别）
- [ ] 实现 `CapabilityGapDetector`（能力缺口检测）
- [ ] Scheduler 定时分析 + 结果存入 Vault

### Phase 5: 跨节点进化
- [ ] 实现 `CapabilityPlanner`（LLM 拆解能力到各节点组件）
- [ ] 实现 `ComponentGenerator`（为每个节点生成代码/配置）
- [ ] 实现 `DistributedDeployer`（MQTT RPC 逐节点部署 + 回滚）
- [ ] MacBook 增加 `evolution_deploy_component` tool
- [ ] 端到端验证 + MeshRegistry 自动更新
- [ ] 审计日志扩展

### Phase 6: iPhone Mobile Node（可选增强）
- [ ] 创建 iOS App 项目（Swift + SwiftUI）
- [ ] MQTT over WebSocket 连接
- [ ] iOS 原生能力 (Camera, Mic, Location, HealthKit)
- [ ] APNs 推送通知集成
- [ ] 结果展示 UI
- [ ] 集成测试：端到端跨三节点场景

---

## 11. 技术选型总结

| 组件 | 选型 | 理由 |
|------|------|------|
| 节点间通信 | MQTT 5.0 (Mosquitto / EMQX) | 会话持久、低带宽、WebSocket、QoS |
| 能力描述 | Node Card (A2A 启发) | 结构化能力声明、自动发现 |
| 任务编排 | 扩展现有 Orchestrator | 复用 Nexus 已有的 Session/Run 管理 |
| macOS 工具 | Python + pyobjc/subprocess | 复用 Nexus Python 生态 |
| iOS App | Swift + SwiftUI | 原生能力访问、性能、推送 |
| MQTT Broker | Mosquitto（Phase 0） / EMQX（后续） | Phase 0 先用轻量 Broker 跑通网络，后续再按规模升级 |
| 本地 LLM | Ollama | 已有生态、多模型支持、简单 API |
| 安全 | mTLS + MQTT ACL | 节点认证 + 工具级权限控制 |

---

## 12. 与现有 Nexus 的关系

这个架构是 Nexus 的**扩展**，不是替代：

```
现有 Nexus (Ubuntu)
  │
  ├─ orchestrator.py      → 扩展为 mesh-aware Orchestrator
  ├─ agent/core.py        → 不变，每个节点有自己的 tool loop
  ├─ agent/tool_registry  → 扩展，增加 RemoteToolProxy
  ├─ provider/gateway.py  → 不变，每个节点有自己的 Gateway
  ├─ evolution/           → 扩展，增加 CrossNodeEvolutionManager
  ├─ channel/             → 不变，Feishu/Web 仍连 Hub
  │
  新增:
  ├─ mesh/registry.py     — Hub 专有
  ├─ mesh/task_router.py  — Hub 专有
  ├─ mesh/transport.py    — 所有节点
  ├─ mesh/remote_tools.py — 所有节点
  ├─ mesh/evolution.py    — Hub 专有
  ├─ mesh/learning.py     — Hub 专有
  │
  新增:
  ├─ edge/                — MacBook Edge Runtime
  └─ NexusMobile/         — iPhone App
```

核心不变量：
- `ProviderGateway` 的接口不变，每个节点各自实例化
- `execute_tool_loop` 不变，工具是本地还是远程对它透明
- `SkillManager` / `CapabilityManager` 不变，是节点级的
- Vault 仍然以 Ubuntu 为 Source of Truth

---

## 13. 关键参考

### 论文
1. **IoA** (arXiv:2407.07061) — 异构 Agent 组网
2. **Agent 协议综述** (arXiv:2505.02279) — MCP/A2A/ACP/ANP 对比
3. **Coral Protocol** (arXiv:2505.00749) — 基于 MCP 的去中心化协作
4. **SolidGPT** (arXiv:2512.08286) — MDP 边缘-云端任务路由
5. **Edge General Intelligence** (arXiv:2507.00672) — 多层 LLM 架构

### 开源项目
1. **MCP Mesh** (dhyansraj/mcp-mesh) — 分布式 Agent 运行时
2. **A2A Protocol** (a2aproject/A2A) — Agent 间通信标准
3. **ANP** (agent-network-protocol) — Agent 网络协议
4. **SPEAR** (lfedgeai/SPEAR) — 边缘-云端协同 Agent 平台
5. **EMQX** — MQTT Broker + MCP over MQTT
