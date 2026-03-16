# Nexus macOS Client V1

## Goal

Build a native macOS client that turns the MacBook Pro into a mesh edge node without
moving the control plane away from the Ubuntu Hub.

V1 focuses on:

- native menu bar presence
- local sidecar supervision
- mesh node registration and heartbeat
- local desktop tools already implemented in `nexus.edge`
- local status visibility for the user

V1 explicitly does not try to finish:

- full chat UI
- Accessibility / AppleScript automation
- iPhone integration
- packaging/signing/notarization

V1.1 extends the shell with:

- approval workflows for risky edge actions
- LaunchAgent install/remove support
- Apple Shortcuts and AppleScript/JXA automation hooks
- a scripted `.app` packaging path for local release bundles

## Design Principles

- Ubuntu remains the only durable control plane.
- macOS is an edge executor plus an operator-facing shell.
- The native shell should be thin; business logic stays in Python where Nexus
  already has mesh/runtime code.
- The native shell must assume `conda ai_assist` is the only supported runtime.

## Topology

```text
┌────────────────────────────────────────────────────────────────────┐
│ macOS Native Shell (SwiftUI/AppKit)                               │
│                                                                    │
│  MenuBarExtra  Dashboard Window  Settings Window                   │
│          │             │                │                          │
│          └────── AppModel / SidecarSupervisor ─────────────────┐   │
└────────────────────────────────────────────────────────────────┬───┘
                                                                 │
                                                                 │ local HTTP
                                                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ Python Sidecar (`python -m nexus.edge.macos_sidecar`)             │
│                                                                    │
│  FastAPI status API                                                │
│  EdgeNodeAgent                                                     │
│  BrowserService (optional)                                         │
│  WorkspaceService                                                  │
│  macOS edge tools                                                  │
└────────────────────────────────────────────────────────────────┬───┘
                                                                 │
                                                                 │ MQTT
                                                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ Ubuntu Hub                                                         │
│  MeshRegistry / TaskRouter / Orchestrator / Knowledge Store        │
└────────────────────────────────────────────────────────────────────┘
```

## Responsibilities

### Swift shell

- own the native UI
- own sidecar process launch / stop / restart
- poll local sidecar health and status
- present current node state, recent events, and available tools
- persist app-local settings such as:
  - Nexus root path
  - node card path
  - sidecar local HTTP port
  - broker overrides
  - conda executable path

### Python sidecar

- create the mesh transport
- load and reconcile the node card
- construct the local edge tool registry
- run `EdgeNodeAgent`
- expose local status via HTTP
- record recent events and task outcomes for UI consumption

### Ubuntu Hub

- remain the source of truth for task planning and task state
- continue to own long-running tasks, knowledge updates, and heavy model usage

## Process Model

The shell launches the sidecar with:

```bash
conda run --no-capture-output -n ai_assist \
  python -m nexus.edge.macos_sidecar \
  --root <NEXUS_ROOT> \
  --node-card-path <NODE_CARD_PATH> \
  --http-port <LOCAL_HTTP_PORT> \
  [broker overrides...]
```

Why this shape:

- it guarantees the `ai_assist` environment is used
- it keeps the Swift app independent from Python packaging details
- it lets the shell supervise the sidecar as a normal child process

## Sidecar Local API

V1 local API:

- `GET /health`
- `GET /status`
- `GET /events`
- `GET /tools`
- `GET /node-card`
- `GET /approvals`
- `POST /approvals/{id}/approve`
- `POST /approvals/{id}/reject`

Process control stays in the Swift shell. Approval decisions flow through the local API.

## UI Structure

### Menu bar

- compact status summary
- start / stop / restart sidecar
- open dashboard
- open settings

### Dashboard window

- sidecar phase
- mesh connection summary
- node identity
- pending approvals with approve/reject actions
- active tools
- recent events

### Settings window

- Nexus root path
- node card path
- conda executable path
- local sidecar port
- broker host / port / transport overrides
- auto-start toggle
- LaunchAgent install/remove status

## Capability Scope In V1

Capabilities expected in the first working version:

- browser automation
- local filesystem read/list
- screen capture / recording
- clipboard read/write
- Apple Shortcuts discovery and execution
- AppleScript / JXA execution behind approval

The sidecar will reconcile the declared node card with actually available tools.
If a tool is unavailable, the capability is trimmed instead of failing startup.

## Failure Model

- If the sidecar dies, the shell stays alive and surfaces the failure.
- If the Mac sleeps, the sidecar process may pause; the Hub still owns task truth.
- If the broker is unreachable, the sidecar stays up in degraded mode and exposes
  the error through the local API.

## Test Strategy

V1 verification includes:

- Python unit tests for the sidecar runtime and status API
- Python approval-flow integration tests
- Swift package build
- Swift unit tests for command construction / LaunchAgent plist generation

Interactive UI behavior is manually inspected; it is not yet covered by automated UI tests.

## Packaging

The app can be bundled as a standalone menu bar `.app` with:

```bash
cd apps/macos
./scripts/package_app.sh
```

The script builds the Swift package in release mode, assembles `dist/NexusMac.app`,
and injects bundle metadata from `Resources/Info.plist.template`.
