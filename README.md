# Nexus / 星策

**Personal AI Agent Runtime — a multi-node distributed AI assistant that extends across all your devices.**

Nexus is a personal AI Agent Runtime that coordinates across multiple nodes (Hub + Edge + Mobile) via an MQTT-based mesh network. It's not a generic chatbot — it's a persistent, tool-wielding, self-evolving personal execution engine.

> 📖 [中文文档 / Chinese Documentation](./README_CN.md)

---

## Key Features

### Multi-Node Mesh Network

```
┌─────────────────────────────────────────────────────────┐
│                    Nexus Mesh                            │
│                                                          │
│   ┌──────────┐   MQTT    ┌──────────┐   MQTT    ┌─────┐ │
│   │ Hub      │◄────────►│ MacBook  │           │ iOS │ │
│   │ (Ubuntu) │           │  (Edge)  │           │     │ │
│   └────┬─────┘           └──────────┘           └─────┘ │
│        │                  AppleScript                    │
│   ┌────┴─────┐            Browser                       │
│   │ Feishu   │            Screenshot                    │
│   │ Web UI   │            Clipboard                     │
│   └──────────┘            Shortcuts                     │
└─────────────────────────────────────────────────────────┘
```

- **Hub (Ubuntu Server)** — Central brain: LLM inference, knowledge base, document management, task routing
- **Edge (MacBook)** — Local executor: browser automation, AppleScript, screen capture, clipboard, local files
- **Mobile (iPhone)** — Mobile node: camera, location, push notifications *(planned)*

The Hub coordinates all nodes via MQTT. The TaskRouter automatically dispatches tasks to whichever node has the required capabilities.

### Agent Runtime

- **Tool-calling Loop** — LLM-driven multi-step tool execution with auto-retry and context compression
- **Tool Governance** — Allowlist/blocklist, risk levels, rate limits, approval workflows
- **Subagent Delegation** — Spawn sub-agents for background long-running tasks
- **Task DAG** — Dependency-aware structured task management

### Three-Layer Knowledge System

- **Vault** — Markdown files as the canonical source of truth, with Notion-style block editing
- **Structural Index** — SQLite-backed page tree, backlinks, and collections
- **Retrieval & Memory** — FTS5 full-text search + episodic memory with time-decay weighting

### Controlled Self-Evolution

- **Skill System** — Discoverable, installable, hot-swappable skill packages that extend the agent's capabilities at runtime
- **Capability Lifecycle** — `sandbox → verify → promote → rollback` with full audit trail
- **Evolution Audit** — Every change logged for traceability and rollback

### Multi-Channel Access

- **Feishu / Lark** — Long-connection mode with rich text, file, and voice message support
- **Web UI** — React + TipTap rich text editor with WebSocket real-time communication
- **macOS Menu Bar** — Native SwiftUI app with local/Hub dual-mode execution

---

## Architecture

```
nexus/
├── nexus/                    # Python backend
│   ├── agent/                # Agent Runtime — tool loop, policies, subagents
│   ├── api/                  # FastAPI application entry
│   ├── channel/              # Channel adapters (Feishu, Web)
│   ├── edge/                 # Edge node — macOS Sidecar, local tools
│   ├── evolution/            # Self-evolution — Skill/Capability lifecycle
│   ├── knowledge/            # Knowledge system — Vault, retrieval, memory
│   ├── mesh/                 # Mesh network — MQTT, TaskRouter, remote tool proxy
│   ├── provider/             # LLM Provider Gateway (multi-model, health check, failover)
│   ├── services/             # Base services (browser, workspace)
│   └── shared/               # Shared config & utilities
├── apps/
│   └── macos/                # SwiftUI macOS menu bar app
├── web/                      # React Web UI
├── config/                   # Configuration files (.example templates)
├── deploy/                   # Deployment config (systemd, nginx, MQTT broker)
├── skills/                   # Installed skill packages
├── skill_registry/           # Installable skill registry
├── capabilities/             # Capability definitions
├── tests/                    # Test suite
└── docs/                     # Architecture docs
```

---

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 18+ (for Web UI)
- MQTT Broker (Mosquitto recommended)
- At least one LLM API key (Moonshot/Kimi, Qwen, OpenAI, or any OpenAI-compatible API)

### 1. Install

```bash
git clone https://github.com/maths369/Nexus.git
cd Nexus

# Create Python environment
conda create -n nexus python=3.11
conda activate nexus
pip install -e ".[dev,browser]"

# Install Web UI dependencies
cd web && npm ci && cd ..
```

### 2. Configure

```bash
# Copy config templates
cp .env.example .env
cp config/app.yaml.example config/app.yaml
cp config/node_cards/hub-server.example.yaml config/node_cards/my-hub.yaml

# Edit .env with your API keys
vim .env

# Edit config/app.yaml to adjust settings
vim config/app.yaml
```

### 3. Start the Hub

```bash
# Start MQTT Broker (if not already running)
mosquitto -c deploy/mosquitto/mosquitto.conf -d

# Start Nexus API
python -m nexus serve --host 0.0.0.0 --port 8000

# Start Web UI (in another terminal)
cd web && npm run dev
```

### 4. Start an Edge Node (MacBook)

```bash
# Copy and configure edge node card
cp config/node_cards/macbook-edge.example.yaml config/node_cards/my-macbook.yaml
vim config/node_cards/my-macbook.yaml

# Start Sidecar
python -m nexus edge --node-card config/node_cards/my-macbook.yaml
```

Or use the macOS Menu Bar App (`apps/macos/`):

```bash
cd apps/macos
xcodebuild -scheme NexusMac -configuration Release build
```

---

## LLM Provider Support

Nexus uses the OpenAI-compatible API protocol and supports multiple providers with automatic failover:

| Provider | Example Models | Config |
|----------|---------------|--------|
| Moonshot / Kimi | kimi-k2.5 | `provider_type: moonshot` |
| Qwen / DashScope | qwen3.5-397b-a17b | `provider_type: qwen` |
| OpenAI | gpt-4o | `provider_type: openai` |
| Ollama (local) | qwen2.5:72b | `via: local` |
| Any OpenAI-compatible API | — | Set `base_url` + `api_key` |

---

## Mesh Network

Nexus Mesh coordinates multi-node collaboration via MQTT:

1. **Node Registration** — Each node declares its capabilities and resources via a Node Card (YAML)
2. **TaskRouter** — Hub detects required capabilities and routes tasks to the best online node
3. **Agent Loop Dispatch** — For multi-step tasks (e.g., browser automation), Hub delegates the entire sub-task to the Edge node's local LLM for autonomous execution
4. **Journal Sync** — Edge nodes periodically sync execution journals back to Hub

### Example: Open Chrome on MacBook via Feishu

```
User (Feishu) → "Open Chrome on my MacBook"
  → Hub receives message
  → TaskRouter detects apple_automation capability → routes to MacBook
  → Hub LLM calls mesh_dispatch tool
  → MacBook receives task → local LLM calls run_applescript
  → Chrome opens → result synced back to Hub → Feishu replies to user
```

---

## Testing

```bash
python -m pytest tests/ -v
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11, FastAPI, asyncio |
| LLM | OpenAI-compatible API (Kimi, Qwen, GPT-4o, Ollama) |
| Messaging | MQTT (aiomqtt), WebSocket |
| Storage | SQLite (FTS5), Markdown (Vault) |
| Web UI | React, TypeScript, TipTap, Vite |
| macOS App | SwiftUI, Combine |
| IM | Feishu / Lark SDK (long-connection) |
| Deployment | systemd, nginx, Docker |

---

## Roadmap

- [ ] iPhone mobile node (camera, location, push notifications)
- [ ] Voice interaction via macOS Menu Bar
- [ ] Multi-user support
- [ ] Plugin marketplace for community skills
- [ ] End-to-end encryption for mesh communication

---

## License

MIT
