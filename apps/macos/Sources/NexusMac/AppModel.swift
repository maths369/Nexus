import AppKit
import Foundation
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    @Published var settings: SidecarSettings
    @Published var commandDraft: String = ""
    @Published private(set) var supervisorState: SidecarSupervisor.State = .stopped
    @Published private(set) var health: SidecarHealthResponse?
    @Published private(set) var hubHealth: HubHealthResponse?
    @Published private(set) var snapshot: SidecarStatusSnapshot?
    @Published private(set) var conversation: [HubConversationEntry] = []
    @Published private(set) var meshTrace: [MeshTraceEntry] = []
    @Published private(set) var isSendingCommand = false
    @Published private(set) var recentLogs: [String] = []
    @Published private(set) var lastError: String?
    @Published private(set) var launchAgentStatus: LaunchAgentStatus?
    @Published private(set) var permissionChecks: [PermissionCheck] = PermissionDiagnostics.snapshot()

    private let supervisor = SidecarSupervisor()
    private var pollTask: Task<Void, Never>?
    private var commandTask: Task<Void, Never>?
    private var pollTick = 0
    private var promptedApprovalIDs: Set<String> = []
    private var isPresentingApprovalPrompt = false
    private var importedEventIDs: Set<String> = []
    private var lastTransportConnected: Bool?
    private var lastPhase: String?
    private var lastKnownLocalAPIReachable: Bool?
    private var lastKnownHubReachable: Bool?
    private var lastObservedHubState: HubConnectivityState?
    private var lastObservedHubAPIHealthy: Bool?
    private var lastObservedHubRuntimeReady: Bool?
    private var lastObservedLocalError: String?

    init() {
        self.settings = SidecarSettings.load()
        supervisor.onOutput = { [weak self] line in
            self?.appendLog(line)
        }
        supervisor.onStateChange = { [weak self] state in
            self?.supervisorState = state
            self?.appendTrace(
                lane: .engine,
                title: "Sidecar supervisor \(state.traceLabel)",
                detail: "The macOS shell changed the local engine supervisor state to \(state.traceLabel)."
            )
        }
        supervisor.onFailure = { [weak self] error in
            self?.lastError = error
            self?.appendTrace(
                lane: .error,
                title: "Sidecar supervisor failure",
                detail: error
            )
        }

        startPolling()
        Task {
            await refreshLaunchAgentStatus()
            await refreshHubHealth()
            await refreshPermissionChecks()
        }
        if settings.autoStartSidecar {
            Task { [weak self] in
                self?.startSidecar()
                await self?.refreshStatus()
            }
        } else {
            Task { await refreshStatus() }
        }
    }

    deinit {
        pollTask?.cancel()
        commandTask?.cancel()
    }

    func persistSettings() {
        settings.save()
    }

    func startSidecar() {
        appendTrace(
            lane: .engine,
            title: "Start requested",
            detail: "Nexus asked the local engine to start and connect this Mac to the Mesh."
        )
        do {
            try supervisor.start(settings: settings)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            appendTrace(
                lane: .error,
                title: "Failed to start local engine",
                detail: error.localizedDescription
            )
        }
    }

    func stopSidecar() {
        appendTrace(
            lane: .engine,
            title: "Stop requested",
            detail: "Nexus asked the local engine to stop."
        )
        supervisor.stop()
    }

    func restartSidecar() {
        appendTrace(
            lane: .engine,
            title: "Restart requested",
            detail: "Nexus asked the local engine to restart with the current settings."
        )
        do {
            try supervisor.restart(settings: settings)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            appendTrace(
                lane: .error,
                title: "Failed to restart local engine",
                detail: error.localizedDescription
            )
        }
    }

    func refreshStatus() async {
        let client = SidecarAPIClient(host: settings.localHost, port: settings.localPort)
        do {
            let health = try await client.health()
            let snapshot = try await client.status()
            self.health = health
            self.snapshot = snapshot
            self.lastError = snapshot.lastError
            syncSnapshotTrace(snapshot)
            if lastKnownLocalAPIReachable != true {
                appendTrace(
                    lane: .engine,
                    title: "Local sidecar reachable",
                    detail: "The macOS shell can read live state from \(settings.localHost):\(settings.localPort)."
                )
            }
            lastKnownLocalAPIReachable = true
            await presentApprovalPromptIfNeeded(from: snapshot)
        } catch {
            self.health = nil
            if lastKnownLocalAPIReachable != false {
                appendTrace(
                    lane: .error,
                    title: "Local sidecar unreachable",
                    detail: "Nexus could not read the local sidecar at \(settings.localHost):\(settings.localPort): \(error.localizedDescription)"
                )
            }
            lastKnownLocalAPIReachable = false
            if !supervisor.isRunning {
                self.snapshot = nil
            }
        }

        pollTick += 1
        if pollTick % 5 == 0 {
            await refreshHubHealth()
            await refreshPermissionChecks()
        }
    }

    func refreshHubHealth() async {
        let client = HubAPIClient(host: settings.resolvedHubAPIHost, port: settings.resolvedHubAPIPort)
        do {
            hubHealth = try await client.health()
            if lastKnownHubReachable != true {
                appendTrace(
                    lane: .hub,
                    title: "Ubuntu Hub reachable",
                    detail: "The Hub health check succeeded at \(settings.resolvedHubAPIHost):\(settings.resolvedHubAPIPort)."
                )
            }
            lastKnownHubReachable = true
        } catch {
            hubHealth = nil
            if lastKnownHubReachable != false {
                appendTrace(
                    lane: .error,
                    title: "Ubuntu Hub unreachable",
                    detail: "The Hub health check failed at \(settings.resolvedHubAPIHost):\(settings.resolvedHubAPIPort): \(error.localizedDescription)"
                )
            }
            lastKnownHubReachable = false
        }
    }

    func refreshPermissionChecks() async {
        permissionChecks = PermissionDiagnostics.snapshot()
    }

    func requestPermission(_ check: PermissionCheck) async {
        switch check.state {
        case .denied:
            PermissionDiagnostics.openSystemSettings(for: check)
        case .granted:
            break
        case .needsPrompt, .unavailable:
            await PermissionDiagnostics.requestPermission(for: check.id)
        }
        try? await Task.sleep(for: .milliseconds(350))
        await refreshPermissionChecks()
    }

    func approveApproval(id: String, comment: String? = nil) async {
        appendTrace(
            lane: .approval,
            title: "Approval granted",
            detail: "Approved pending operation \(id)."
        )
        let client = SidecarAPIClient(host: settings.localHost, port: settings.localPort)
        do {
            _ = try await client.approveApproval(id: id, comment: comment)
            await refreshStatus()
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            appendTrace(
                lane: .error,
                title: "Approval submit failed",
                detail: error.localizedDescription
            )
        }
    }

    func rejectApproval(id: String, comment: String? = nil) async {
        appendTrace(
            lane: .approval,
            title: "Approval rejected",
            detail: "Rejected pending operation \(id)."
        )
        let client = SidecarAPIClient(host: settings.localHost, port: settings.localPort)
        do {
            _ = try await client.rejectApproval(id: id, comment: comment)
            await refreshStatus()
            lastError = nil
        } catch {
            lastError = error.localizedDescription
            appendTrace(
                lane: .error,
                title: "Rejection submit failed",
                detail: error.localizedDescription
            )
        }
    }

    func sendCommand() {
        let trimmed = commandDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !isSendingCommand else { return }

        conversation.append(HubConversationEntry(role: .user, kind: .command, content: trimmed))
        appendTrace(
            lane: .command,
            title: "Command sent via \(settings.commandMode.label)",
            detail: trimmed
        )
        commandDraft = ""
        isSendingCommand = true
        lastError = nil
        commandTask?.cancel()

        let mode = settings.commandMode

        commandTask = Task { [weak self] in
            guard let self else { return }

            switch mode {
            case .hub:
                if self.isHubReadyForDispatch {
                    await self.sendViaHub(trimmed)
                } else {
                    self.queueHubRetry(
                        content: trimmed,
                        waitingMessage: "Ubuntu Hub is offline. Nexus will wait for it to come back before sending this command."
                    )
                }
            case .local:
                await self.sendViaLocal(trimmed)
            case .auto:
                if self.isHubReadyForDispatch {
                    await self.sendViaHub(trimmed)
                } else if self.shouldFallbackToLocalWhenHubUnavailable(task: trimmed) {
                    self.conversation.append(
                        HubConversationEntry(role: .system, kind: .status, content: "Hub unavailable, executing locally on this Mac...")
                    )
                    self.appendTrace(
                        lane: .hub,
                        title: "Auto mode fell back to This Mac",
                        detail: "Hub is not ready, and the command looks satisfiable with this Mac's local tools."
                    )
                    await self.sendViaLocal(trimmed)
                } else {
                    self.conversation.append(
                        HubConversationEntry(role: .system, kind: .status, content: "Hub unavailable, waiting for Ubuntu Hub because this command likely needs shared knowledge or remote services...")
                    )
                    self.appendTrace(
                        lane: .hub,
                        title: "Auto mode waiting for Hub",
                        detail: "Hub is not ready, and the command looks like it needs the shared control plane."
                    )
                    self.queueHubRetry(
                        content: trimmed,
                        waitingMessage: "Hub is not ready yet. Nexus will retry automatically when Ubuntu Hub is back."
                    )
                }
            }

            self.isSendingCommand = false
        }
    }

    private func queueHubRetry(content: String, waitingMessage: String) {
        conversation.append(
            HubConversationEntry(role: .system, kind: .status, content: waitingMessage)
        )
        appendTrace(
            lane: .hub,
            title: "Queued until Hub recovers",
            detail: waitingMessage
        )
        Task { [weak self] in
            guard let self else { return }
            let recovered = await self.waitForHubRecovery(timeoutSeconds: 120)
            if recovered {
                self.conversation.append(
                    HubConversationEntry(role: .system, kind: .status, content: "Ubuntu Hub is reachable again. Retrying the command now...")
                )
                self.appendTrace(
                    lane: .hub,
                    title: "Hub recovered",
                    detail: "Retrying the queued command through Ubuntu Hub."
                )
                await self.sendViaHub(content)
            } else {
                let message = "Ubuntu Hub did not recover within 2 minutes. Retry later, or switch to This Mac if the task only needs local resources."
                self.lastError = message
                self.conversation.append(
                    HubConversationEntry(role: .system, kind: .error, content: message)
                )
                self.appendTrace(
                    lane: .error,
                    title: "Hub wait timed out",
                    detail: message
                )
            }
        }
    }

    private func waitForHubRecovery(timeoutSeconds: Double) async -> Bool {
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        while Date() < deadline {
            await refreshStatus()
            await refreshHubHealth()
            if isHubReadyForDispatch {
                return true
            }
            try? await Task.sleep(for: .seconds(2))
        }
        return false
    }

    private func sendViaHub(_ content: String) async {
        let client = HubAPIClient(host: settings.resolvedHubAPIHost, port: settings.resolvedHubAPIPort)
        let senderID = hubSenderID
        do {
            try await client.sendMessage(content: content, senderID: senderID) { [weak self] entry in
                self?.conversation.append(entry)
                self?.appendTrace(
                    lane: entry.traceLane,
                    title: entry.traceTitle,
                    detail: entry.content,
                    metadata: entry.traceMetadata,
                    sessionID: entry.sessionID
                )
            }
            self.hubHealth = HubHealthResponse(status: "ok", version: self.hubHealth?.version ?? "unknown")
        } catch {
            if settings.commandMode == .auto {
                self.conversation.append(
                    HubConversationEntry(role: .system, kind: .status, content: "Hub failed, falling back to local execution...")
                )
                appendTrace(
                    lane: .hub,
                    title: "Hub execution failed, falling back",
                    detail: error.localizedDescription
                )
                await sendViaLocal(content)
            } else {
                let message = error.localizedDescription
                self.lastError = message
                self.conversation.append(
                    HubConversationEntry(role: .system, kind: .error, content: message)
                )
                appendTrace(
                    lane: .error,
                    title: "Hub command failed",
                    detail: message
                )
            }
        }
    }

    private func sendViaLocal(_ content: String) async {
        let client = SidecarAPIClient(host: settings.localHost, port: settings.localPort)
        do {
            let result = try await client.localCommand(task: content)
            let output = result.success
                ? result.output
                : "Local execution failed: \(result.error ?? "unknown error")"
            let kind: HubConversationEntry.Kind = result.success ? .result : .error
            self.conversation.append(
                HubConversationEntry(
                    role: .assistant,
                    kind: kind,
                    content: output
                )
            )
            appendTrace(
                lane: result.success ? .node : .error,
                title: result.success ? "Local execution completed" : "Local execution failed",
                detail: output
            )
            if !result.success {
                self.lastError = result.error
            }
        } catch {
            let message = error.localizedDescription
            self.lastError = message
            self.conversation.append(
                HubConversationEntry(role: .system, kind: .error, content: "Local execution unavailable: \(message)")
            )
            appendTrace(
                lane: .error,
                title: "Local execution unavailable",
                detail: message
            )
        }
    }

    func clearConversation() {
        conversation.removeAll()
    }

    func clearMeshTrace() {
        meshTrace.removeAll()
        importedEventIDs.removeAll()
    }

    func quitApplication() {
        Task {
            stopSidecar()
            if let error = await LaunchAgentManager.unloadForUserQuit() {
                lastError = error
            }
            NSApplication.shared.terminate(nil)
        }
    }

    func refreshLaunchAgentStatus() async {
        launchAgentStatus = await LaunchAgentManager.status()
    }

    func installLaunchAgent() async {
        if let error = await LaunchAgentManager.install() {
            lastError = error
        } else {
            lastError = nil
        }
        await refreshLaunchAgentStatus()
    }

    func removeLaunchAgent() async {
        if let error = await LaunchAgentManager.uninstall() {
            lastError = error
        } else {
            lastError = nil
        }
        await refreshLaunchAgentStatus()
    }

    var statusIconName: String {
        if let snapshot {
            switch snapshot.phase {
            case "running":
                if !snapshot.pendingApprovals.isEmpty {
                    return "checklist.unchecked"
                }
                switch snapshot.hub.connectivityState {
                case .connected:
                    return "dot.radiowaves.up.forward"
                case .brokerOnly:
                    return "externaldrive.connected.to.line.below"
                case .reconnecting:
                    return "arrow.triangle.2.circlepath"
                case .localOnly:
                    return "wifi.exclamationmark"
                }
            case "starting":
                return "arrow.triangle.2.circlepath"
            case "error":
                return "exclamationmark.triangle.fill"
            default:
                break
            }
        }
        switch supervisorState {
        case .running:
            return "dot.radiowaves.up.forward"
        case .starting:
            return "arrow.triangle.2.circlepath"
        case .stopped:
            return "pause.circle"
        }
    }

    var statusLabel: String {
        if let snapshot {
            if !snapshot.pendingApprovals.isEmpty {
                return "\(snapshot.pendingApprovals.count) approval\(snapshot.pendingApprovals.count == 1 ? "" : "s") waiting"
            }
            switch snapshot.phase {
            case "running":
                switch snapshot.hub.connectivityState {
                case .connected:
                    if let name = snapshot.nodeCard?.displayName {
                        return "\(name) connected"
                    }
                    return "This Mac is connected"
                case .brokerOnly:
                    return "Broker connected, Hub not ready"
                case .reconnecting:
                    return "Reconnecting to Ubuntu Hub"
                case .localOnly:
                    return "Local engine running"
                }
            case "starting":
                return "Local engine starting"
            case "error":
                return "Local engine error"
            default:
                return "Local engine stopped"
            }
        }
        switch supervisorState {
        case .running:
            return "Local engine starting"
        case .starting:
            return "Local engine starting"
        case .stopped:
            return "Local engine stopped"
        }
    }

    var toolCount: Int {
        snapshot?.tools.count ?? 0
    }

    var recentEventCount: Int {
        snapshot?.recentEvents.count ?? 0
    }

    var pendingApprovalCount: Int {
        snapshot?.pendingApprovals.count ?? 0
    }

    var primaryHubStatusLabel: String {
        if let hub = snapshot?.hub {
            switch hub.connectivityState {
            case .connected:
                return "Ubuntu Hub connected"
            case .brokerOnly:
                return "Hub API or registry unavailable"
            case .localOnly:
                return "Ubuntu Hub unavailable"
            case .reconnecting:
                return "Reconnecting to Ubuntu Hub"
            }
        }
        guard let hubHealth else {
            return "Ubuntu Hub unavailable"
        }
        return hubHealth.status == "ok" ? "Ubuntu Hub reachable" : "Ubuntu Hub degraded"
    }

    var canSendCommand: Bool {
        !commandDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isSendingCommand
    }

    var sendButtonLabel: String {
        if isSendingCommand { return "Sending..." }
        switch settings.commandMode {
        case .auto: return isHubReadyForDispatch ? "Send to Hub" : "Auto Decide"
        case .hub: return "Send to Hub"
        case .local: return "Run on Mac"
        }
    }

    var commandModeDescription: String {
        switch settings.commandMode {
        case .auto:
            return "Try Ubuntu Hub first. If it is down, Nexus runs obvious Mac-local tasks here and waits for Hub for shared work."
        case .hub:
            return "Always use Ubuntu Hub. If Hub is offline, Nexus will wait and retry automatically."
        case .local:
            return "Execute on this Mac using its local tools and API LLM without depending on Ubuntu Hub."
        }
    }

    var hubConnectivityState: HubConnectivityState? {
        snapshot?.hub.connectivityState ?? hubHealth.map { _ in .connected }
    }

    var isHubReadyForDispatch: Bool {
        snapshot?.hub.connectivityState.canDispatchToHub ?? (hubHealth != nil)
    }

    private func shouldFallbackToLocalWhenHubUnavailable(task: String) -> Bool {
        let compact = task.lowercased()
        let localTokens = [
            "this mac",
            "macbook",
            "chrome",
            "safari",
            "finder",
            "clipboard",
            "screenshot",
            "screen shot",
            "shortcut",
            "apple script",
            "打开",
            "启动",
            "激活",
            "这台mac",
            "本地",
            "截屏",
            "截图",
            "录屏",
            "剪贴板",
            "快捷指令",
            "applescript",
            "文件夹",
            "访达"
        ]
        let sharedHubTokens = [
            "knowledge base",
            "vault",
            "rag",
            "research",
            "analyze",
            "summarize",
            "search",
            "知识库",
            "行业研究",
            "搜索",
            "总结",
            "分析",
            "趋势",
            "战略",
            "报告"
        ]
        let mentionsLocal = localTokens.contains { compact.contains($0) }
        let mentionsSharedHub = sharedHubTokens.contains { compact.contains($0) }
        return mentionsLocal && !mentionsSharedHub
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refreshStatus()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func appendLog(_ line: String) {
        recentLogs.append(line)
        if recentLogs.count > 120 {
            recentLogs.removeFirst(recentLogs.count - 120)
        }
        appendTrace(
            lane: .engine,
            title: "Local engine log",
            detail: line
        )
    }

    private func syncSnapshotTrace(_ snapshot: SidecarStatusSnapshot) {
        if lastPhase != snapshot.phase {
            appendTrace(
                lane: .engine,
                title: "Local engine phase changed",
                detail: "The Mac node phase is now \(snapshot.phase)."
            )
            lastPhase = snapshot.phase
        }

        if lastTransportConnected != snapshot.transportConnected {
            appendTrace(
                lane: snapshot.transportConnected ? .node : .error,
                title: snapshot.transportConnected ? "Mesh transport connected" : "Mesh transport disconnected",
                detail: snapshot.transportConnected
                    ? "This Mac is connected to \(snapshot.mesh.brokerHost):\(snapshot.mesh.brokerPort) over \(snapshot.mesh.transport)."
                    : "This Mac lost its Mesh transport connection to \(snapshot.mesh.brokerHost):\(snapshot.mesh.brokerPort)."
            )
            lastTransportConnected = snapshot.transportConnected
        }

        if lastObservedHubState != snapshot.hub.connectivityState {
            appendTrace(
                lane: snapshot.hub.connectivityState == .connected ? .hub : .error,
                title: "Hub connectivity state changed",
                detail: "This Mac now sees the Hub as \(snapshot.hub.connectivityState.label.lowercased())."
            )
            lastObservedHubState = snapshot.hub.connectivityState
        }

        if lastObservedHubAPIHealthy != snapshot.hub.apiHealthy {
            appendTrace(
                lane: snapshot.hub.apiHealthy ? .hub : .error,
                title: snapshot.hub.apiHealthy ? "Hub API reachable" : "Hub API unreachable",
                detail: snapshot.hub.apiHealthy
                    ? "The sidecar confirmed the Hub API at \(snapshot.hub.apiHost):\(snapshot.hub.apiPort)."
                    : "The sidecar could not reach the Hub API at \(snapshot.hub.apiHost):\(snapshot.hub.apiPort)."
            )
            lastObservedHubAPIHealthy = snapshot.hub.apiHealthy
        }

        if lastObservedHubRuntimeReady != snapshot.hub.runtimeReady {
            appendTrace(
                lane: snapshot.hub.runtimeReady ? .hub : .error,
                title: snapshot.hub.runtimeReady ? "Hub runtime ready" : "Hub runtime not ready",
                detail: snapshot.hub.runtimeReady
                    ? "The Hub registry reports its runtime as ready for dispatch."
                    : (snapshot.hub.lastError ?? "The Hub API is up, but the runtime is not yet ready for dispatch.")
            )
            lastObservedHubRuntimeReady = snapshot.hub.runtimeReady
        }

        if lastObservedLocalError != snapshot.lastError, let lastError = snapshot.lastError, !lastError.isEmpty {
            appendTrace(
                lane: .error,
                title: "Local engine reported an error",
                detail: lastError
            )
        }
        lastObservedLocalError = snapshot.lastError

        for event in snapshot.recentEvents.sorted(by: { $0.timestamp < $1.timestamp }) {
            guard importedEventIDs.insert(event.id).inserted else {
                continue
            }
            appendTrace(
                lane: traceLane(for: event),
                title: event.message,
                detail: traceDetail(for: event),
                metadata: traceMetadata(for: event),
                timestamp: Date(timeIntervalSince1970: event.timestamp)
            )
        }
    }

    private func presentApprovalPromptIfNeeded(from snapshot: SidecarStatusSnapshot) async {
        guard !isPresentingApprovalPrompt else {
            return
        }

        let pending = snapshot.pendingApprovals.filter { !promptedApprovalIDs.contains($0.id) }
        guard let approval = pending.first else {
            return
        }

        promptedApprovalIDs.insert(approval.id)
        isPresentingApprovalPrompt = true
        appendTrace(
            lane: .approval,
            title: "Approval prompt shown",
            detail: approval.reason,
            metadata: "tool=\(approval.toolName) · risk=\(approval.riskLevel)"
        )

        let response = presentApprovalAlert(for: approval)
        switch response {
        case .alertFirstButtonReturn:
            await approveApproval(id: approval.id)
        case .alertSecondButtonReturn:
            await rejectApproval(id: approval.id, comment: "Rejected from native approval alert")
        default:
            break
        }

        isPresentingApprovalPrompt = false
    }

    private func presentApprovalAlert(for approval: PendingApprovalSnapshot) -> NSApplication.ModalResponse {
        NSApp.activate(ignoringOtherApps: true)

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Nexus needs approval to use this Mac"
        alert.informativeText = approvalAlertBody(for: approval)
        alert.addButton(withTitle: "Approve")
        alert.addButton(withTitle: "Reject")
        alert.buttons.first?.hasDestructiveAction = false
        alert.buttons.last?.hasDestructiveAction = true
        return alert.runModal()
    }

    private func approvalAlertBody(for approval: PendingApprovalSnapshot) -> String {
        var lines = [
            "Tool: \(approval.toolName)",
            "Risk: \(approval.riskLevel.uppercased())",
            approval.reason,
        ]

        if !approval.arguments.isEmpty {
            let arguments = approval.arguments
                .map { "\($0.key)=\($0.value.description)" }
                .sorted()
                .joined(separator: " · ")
            lines.append("Arguments: \(arguments)")
        }

        if approval.toolName == "run_applescript" {
            lines.append("After you approve inside Nexus, macOS may still show a one-time Automation permission prompt.")
        }

        return lines.joined(separator: "\n\n")
    }

    private var hubSenderID: String {
        if let nodeID = snapshot?.nodeCard?.nodeID, !nodeID.isEmpty {
            return "macos:\(nodeID)"
        }
        let fallback = Host.current().localizedName?.replacingOccurrences(of: " ", with: "-").lowercased() ?? "this-mac"
        return "macos:\(fallback)"
    }

    private func appendTrace(
        lane: MeshTraceEntry.Lane,
        title: String,
        detail: String,
        metadata: String? = nil,
        sessionID: String? = nil,
        timestamp: Date = .now
    ) {
        meshTrace.append(
            MeshTraceEntry(
                timestamp: timestamp,
                lane: lane,
                title: title,
                detail: detail,
                metadata: metadata,
                sessionID: sessionID
            )
        )
        if meshTrace.count > 400 {
            meshTrace.removeFirst(meshTrace.count - 400)
        }
    }

    private func traceLane(for event: SidecarStatusSnapshot.EventSnapshot) -> MeshTraceEntry.Lane {
        let kind = event.kind.lowercased()
        if event.level.lowercased() == "error" {
            return .error
        }
        if kind.contains("approval") {
            return .approval
        }
        if kind.contains("task") || kind.contains("agent") || kind.contains("rpc") {
            return .node
        }
        return .engine
    }

    private func traceDetail(for event: SidecarStatusSnapshot.EventSnapshot) -> String {
        guard !event.details.isEmpty else {
            return "\(event.kind) · \(event.level)"
        }
        let details = event.details
            .map { "\($0.key)=\($0.value.description)" }
            .sorted()
            .joined(separator: " · ")
        return "\(event.kind) · \(event.level)\n\(details)"
    }

    private func traceMetadata(for event: SidecarStatusSnapshot.EventSnapshot) -> String? {
        let importantKeys = ["task_id", "step_id", "node_id", "source_node", "tool_name", "session_id"]
        let importantValues = importantKeys.compactMap { key -> String? in
            guard let value = event.details[key] else {
                return nil
            }
            return "\(key)=\(value.description)"
        }
        guard !importantValues.isEmpty else {
            return nil
        }
        return importantValues.joined(separator: " · ")
    }
}

private extension SidecarSupervisor.State {
    var traceLabel: String {
        switch self {
        case .stopped:
            return "stopped"
        case .starting:
            return "starting"
        case .running:
            return "running"
        }
    }
}

private extension HubConversationEntry {
    var traceLane: MeshTraceEntry.Lane {
        switch kind {
        case .ack, .status, .note, .clarify:
            return .hub
        case .result:
            return .node
        case .blocked, .error:
            return .error
        case .command:
            return .command
        }
    }

    var traceTitle: String {
        switch kind {
        case .ack:
            return "Hub acknowledged command"
        case .status:
            return "Hub status update"
        case .blocked:
            return "Hub blocked execution"
        case .result:
            return "Hub reported result"
        case .clarify:
            return "Hub requested clarification"
        case .error:
            return "Hub reported error"
        case .note:
            return "Hub note"
        case .command:
            return "Command"
        }
    }

    var traceMetadata: String? {
        guard let sessionID, !sessionID.isEmpty else {
            return nil
        }
        return "session=\(sessionID)"
    }
}
