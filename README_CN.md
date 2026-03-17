# Nexus / 星策

**Personal AI Agent Runtime — 跨设备的个人 AI 助手操作系统**

Nexus 是一个面向个人用户的 AI Agent 运行时，通过多节点协同（Hub + Edge + Mobile）将 AI 助手的能力从云端延伸到你的每一台设备。

它不是一个通用聊天机器人，而是一个有记忆、有工具、可进化的个人执行中枢。

---

## 核心特性

### 多节点 Mesh 网络

```
┌─────────────────────────────────────────────────────────┐
│                    Nexus Mesh                           │
│                                                         │
│   ┌──────────┐   MQTT    ┌──────────┐   MQTT    ┌────┐ │
│   │ Hub      │◄────────►│ MacBook  │           │ iOS│ │
│   │ (Ubuntu) │           │  (Edge)  │           │    │ │
│   └────┬─────┘           └──────────┘           └────┘ │
│        │                  AppleScript                   │
│   ┌────┴─────┐            Browser                      │
│   │ Feishu   │            Screenshot                   │
│   │ Web UI   │            Clipboard                    │
│   └──────────┘            Shortcuts                    │
└─────────────────────────────────────────────────────────┘
```

- **Hub (Ubuntu Server)** — 中枢节点：LLM 推理、知识库、文档管理、任务路由
- **Edge (MacBook)** — 边缘节点：浏览器自动化、AppleScript、截屏、剪贴板、本地文件
- **Mobile (iPhone)** — 移动节点：拍照、定位、推送通知（规划中）

Hub 通过 MQTT 协调所有节点，TaskRouter 自动将任务分配给具备相应能力的节点执行。

### Agent Runtime

- **Tool-calling Loop** — LLM 驱动的多步工具调用循环，支持自动重试和上下文压缩
- **Tool Governance** — 黑白名单、风险等级、频率限制、审批流程
- **Subagent** — 子 Agent 委派，支持后台长时任务
- **Task DAG** — 任务依赖图，结构化任务管理

### 知识系统（三层架构）

- **Vault** — Markdown 文件为规范知识源，Notion-style block 编辑
- **结构索引** — SQLite 维护页面树、反向链接、Collection
- **检索与记忆** — FTS5 全文检索 + 情景记忆（episodic memory），时间衰减权重

### 受控自我进化

- **Skill System** — 可发现、安装、加载的技能包，热插拔扩展 Agent 能力
- **Capability Lifecycle** — `sandbox → verify → promote → rollback` 全生命周期
- **Evolution Audit** — 所有变更记录到审计日志

### 多通道接入

- **飞书 / Lark** — 长连接模式，支持富文本、文件、语音消息
- **Web UI** — React + TipTap 富文本编辑器，WebSocket 实时通信
- **macOS Menu Bar** — SwiftUI 原生菜单栏应用，本地/Hub 双模式

---

## 项目结构

```
nexus/
├── nexus/                    # Python 后端
│   ├── agent/                # Agent Runtime — 工具调用循环、策略、子代理
│   ├── api/                  # FastAPI 应用入口
│   ├── channel/              # 通道适配器（飞书、Web）
│   ├── edge/                 # Edge 节点 — macOS Sidecar、本地工具
│   ├── evolution/            # 自我进化 — Skill/Capability 生命周期
│   ├── knowledge/            # 知识系统 — Vault、检索、记忆
│   ├── mesh/                 # Mesh 网络 — MQTT、TaskRouter、远程工具代理
│   ├── provider/             # LLM Provider Gateway（多模型、健康检查、故障转移）
│   ├── services/             # 基础服务（浏览器、工作区）
│   └── shared/               # 共享配置与工具
├── apps/
│   └── macos/                # SwiftUI macOS 菜单栏应用
├── web/                      # React Web UI
├── config/                   # 配置文件（.example 模板）
├── deploy/                   # 部署配置（systemd、nginx、MQTT broker）
├── skills/                   # 已安装的 Skill 包
├── skill_registry/           # 可安装 Skill 注册表
├── capabilities/             # Capability 定义
├── tests/                    # 测试套件
└── docs/                     # 架构文档
```

---

## 快速开始

### 前置条件

- Python 3.10+
- Node.js 18+ (Web UI)
- MQTT Broker (Mosquitto 推荐)
- 至少一个 LLM API Key（Moonshot/Kimi、通义千问、或任何 OpenAI 兼容 API）

### 1. 安装

```bash
git clone https://github.com/maths369/Nexus.git
cd nexus

# 创建 Python 环境
conda create -n nexus python=3.11
conda activate nexus
pip install -e ".[dev,browser]"

# 安装 Web 依赖
cd web && npm ci && cd ..
```

### 2. 配置

```bash
# 复制配置模板
cp .env.example .env
cp config/app.yaml.example config/app.yaml
cp config/node_cards/hub-server.example.yaml config/node_cards/my-hub.yaml

# 编辑 .env 填入你的 API Key
vim .env

# 编辑 config/app.yaml 调整参数
vim config/app.yaml
```

### 3. 启动 Hub

```bash
# 启动 MQTT Broker（如果还没运行）
mosquitto -c deploy/mosquitto/mosquitto.conf -d

# 启动 Nexus API
python -m nexus serve --host 0.0.0.0 --port 8000

# 启动 Web UI（另一个终端）
cd web && npm run dev
```

### 4. 启动 Edge (MacBook)

```bash
# 复制 Edge 节点卡片
cp config/node_cards/macbook-edge.example.yaml config/node_cards/my-macbook.yaml
# 编辑填入 API Key
vim config/node_cards/my-macbook.yaml

# 启动 Sidecar
python -m nexus edge --node-card config/node_cards/my-macbook.yaml
```

或使用 macOS Menu Bar App（`apps/macos/`）：

```bash
cd apps/macos
swift build
open .build/debug/NexusMac.app
```

---

## LLM Provider 支持

Nexus 使用 OpenAI 兼容的 API 协议，支持：

| Provider | 模型示例 | 配置 |
|----------|---------|------|
| Moonshot / Kimi | kimi-k2.5 | `provider_type: moonshot` |
| 通义千问 / DashScope | qwen3.5-397b-a17b | `provider_type: qwen` |
| OpenAI | gpt-4o | `provider_type: openai` |
| Ollama (本地) | qwen2.5:72b | `via: local` |
| 任何 OpenAI 兼容 API | — | 配置 `base_url` + `api_key` |

支持多 Provider 故障转移、健康检查、自动切换。

---

## Mesh 网络

Nexus Mesh 通过 MQTT 协调多个节点协同工作：

1. **节点注册** — 每个节点通过 Node Card（YAML）声明自己的能力和资源
2. **TaskRouter** — Hub 自动检测任务所需能力，路由到合适的在线节点
3. **Agent Loop Dispatch** — 对于多步任务（如浏览器操作），Hub 将整个子任务委托给 Edge 节点的本地 LLM 自主执行
4. **Journal Sync** — Edge 节点定期将执行日志同步回 Hub

### 示例：通过飞书让 MacBook 打开 Chrome

```
用户(飞书) → "打开MacBook上的Chrome"
  → Hub 接收消息
  → TaskRouter 检测 apple_automation 能力 → 路由到 MacBook
  → Hub LLM 调用 mesh_dispatch 工具
  → MacBook 收到任务 → 本地 LLM 调用 run_applescript
  → Chrome 打开 → 结果回传 Hub → 飞书回复用户
```

---

## 测试

```bash
python -m pytest tests/ -v
```

---

## 技术栈

| 层 | 技术 |
|---|------|
| Backend | Python 3.11, FastAPI, asyncio |
| LLM | OpenAI-compatible API (Kimi, Qwen, GPT-4o, Ollama) |
| Messaging | MQTT (aiomqtt), WebSocket |
| Storage | SQLite (FTS5), Markdown (Vault) |
| Web UI | React, TypeScript, TipTap, Vite |
| macOS App | SwiftUI, Combine |
| IM | Feishu/Lark SDK (长连接) |
| Deployment | systemd, nginx, Docker |

---

## License

MIT
