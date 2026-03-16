import SwiftUI

struct DashboardView: View {
    @ObservedObject var model: AppModel
    var onOpenSettings: (() -> Void)? = nil
    var onOpenMeshTrace: (() -> Void)? = nil

    var body: some View {
        ZStack {
            NexusPanelBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    hero
                    metrics
                    dashboardColumns
                }
                .padding(28)
                .frame(maxWidth: 1180, alignment: .center)
            }
            .scrollIndicators(.hidden)
        }
    }

    private var hero: some View {
        NexusCard("This Mac", eyebrow: "Nexus") {
            HStack(alignment: .top, spacing: 18) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(model.statusLabel)
                        .font(.system(size: 30, weight: .bold, design: .rounded))
                        .foregroundStyle(NexusPalette.textPrimary)
                    Text(heroSummary)
                        .font(.system(size: 14, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 10) {
                    statusBadge
                    HStack(spacing: 10) {
                        Button("Refresh") {
                            Task { await model.refreshStatus() }
                        }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.amber))
                        Button("Open Trace") {
                            onOpenMeshTrace?()
                        }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.ocean))
                        Button("Open Settings") {
                            onOpenSettings?()
                        }
                        .buttonStyle(NexusPrimaryButtonStyle())
                    }
                    .frame(maxWidth: 460)
                }
            }
        }
    }

    private var metrics: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 14) {
                metricTiles
            }
            VStack(spacing: 14) {
                HStack(spacing: 14) {
                    NexusMetricTile(label: "Tools", value: "\(model.toolCount)", tone: NexusPalette.cyan)
                    NexusMetricTile(label: "Approvals", value: "\(model.pendingApprovalCount)", tone: NexusPalette.rose)
                }
                HStack(spacing: 14) {
                    NexusMetricTile(label: "Events", value: "\(model.recentEventCount)", tone: NexusPalette.amber)
                    NexusMetricTile(label: "Phase", value: model.snapshot?.phase.capitalized ?? "Idle", tone: NexusPalette.mint)
                }
            }
        }
    }

    @ViewBuilder
    private var metricTiles: some View {
        NexusMetricTile(label: "Tools", value: "\(model.toolCount)", tone: NexusPalette.cyan)
        NexusMetricTile(label: "Approvals", value: "\(model.pendingApprovalCount)", tone: NexusPalette.rose)
        NexusMetricTile(label: "Events", value: "\(model.recentEventCount)", tone: NexusPalette.amber)
        NexusMetricTile(label: "Phase", value: model.snapshot?.phase.capitalized ?? "Idle", tone: NexusPalette.mint)
    }

    private var dashboardColumns: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .top, spacing: 18) {
                leftColumn
                rightColumn
            }
            VStack(alignment: .leading, spacing: 18) {
                leftColumn
                rightColumn
            }
        }
    }

    private var leftColumn: some View {
        VStack(alignment: .leading, spacing: 18) {
            connectionCard
            permissionsCard
            approvalsCard
            eventsCard
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var rightColumn: some View {
        VStack(alignment: .leading, spacing: 18) {
            capabilityGroupsCard
            toolsCard
            logsCard
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
    }

    private var connectionCard: some View {
        NexusCard("Connection", eyebrow: "How This Mac Connects") {
            if let snapshot = model.snapshot {
                VStack(alignment: .leading, spacing: 10) {
                    NexusKeyValueRow(key: "Node", value: snapshot.nodeCard?.displayName ?? "Unknown")
                    NexusKeyValueRow(key: "Node ID", value: snapshot.nodeCard?.nodeID ?? "Unknown")
                    NexusKeyValueRow(key: "Broker", value: "\(snapshot.mesh.brokerHost):\(snapshot.mesh.brokerPort)")
                    NexusKeyValueRow(key: "Hub API", value: "\(snapshot.hub.apiHost):\(snapshot.hub.apiPort)")
                    NexusKeyValueRow(key: "Hub State", value: snapshot.hub.connectivityState.label)
                    NexusKeyValueRow(key: "API Reachability", value: snapshot.hub.apiHealthy ? "reachable" : "unreachable")
                    NexusKeyValueRow(key: "Hub Runtime", value: snapshot.hub.runtimeReady ? "ready" : "not ready")
                    NexusKeyValueRow(key: "Transport", value: "\(snapshot.mesh.transport) · \(snapshot.transportConnected ? "connected" : "disconnected")")
                    NexusKeyValueRow(key: "State", value: snapshot.phase)
                    NexusKeyValueRow(key: "Running Tasks", value: String(snapshot.activeExecutions))
                }
            } else {
                Text("Local engine status unavailable.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
        }
    }

    private var approvalsCard: some View {
        NexusCard("Pending Approvals", eyebrow: "Needs Your Input") {
            if let snapshot = model.snapshot, !snapshot.pendingApprovals.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(snapshot.pendingApprovals) { approval in
                        dashboardItemCard(tone: NexusPalette.rose.opacity(0.08)) {
                            HStack {
                                Text(approval.toolName)
                                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                                    .foregroundStyle(NexusPalette.textPrimary)
                                Spacer()
                                NexusBadge(text: approval.riskLevel, tone: NexusPalette.rose)
                            }
                            Text(approval.reason)
                                .font(.system(size: 12, weight: .medium, design: .rounded))
                                .foregroundStyle(NexusPalette.textSecondary)
                            if !approval.arguments.isEmpty {
                                Text(approval.arguments.map { "\($0.key)=\($0.value.description)" }.sorted().joined(separator: " · "))
                                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                                    .foregroundStyle(NexusPalette.textSecondary.opacity(0.75))
                            }
                            HStack(spacing: 10) {
                                Button("Approve") {
                                    Task { await model.approveApproval(id: approval.id) }
                                }
                                .buttonStyle(NexusPrimaryButtonStyle())
                                Button("Reject") {
                                    Task { await model.rejectApproval(id: approval.id, comment: "Rejected from macOS shell") }
                                }
                                .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.rose))
                            }
                        }
                    }
                }
            } else {
                Text("Nothing is waiting for your approval.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
        }
    }

    private var permissionsCard: some View {
        NexusCard("Permission Readiness", eyebrow: "What macOS Will Allow") {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(model.permissionChecks) { check in
                    dashboardItemCard(tone: PermissionDiagnostics.stateTone(check.state).opacity(0.08)) {
                        HStack {
                            Text(check.title)
                                .font(.system(size: 15, weight: .semibold, design: .rounded))
                                .foregroundStyle(NexusPalette.textPrimary)
                            Spacer()
                            NexusBadge(text: check.state.label, tone: PermissionDiagnostics.stateTone(check.state))
                        }
                        Text(check.detail)
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                        HStack(spacing: 10) {
                            if let actionTitle = PermissionDiagnostics.actionTitle(for: check) {
                                Button(actionTitle) {
                                    Task { await model.requestPermission(check) }
                                }
                                .buttonStyle(NexusSecondaryButtonStyle(tone: PermissionDiagnostics.stateTone(check.state)))
                            }
                            if check.state != .granted, check.state != .denied, check.systemSettingsPane != nil {
                                Button("Open Settings") {
                                    PermissionDiagnostics.openSystemSettings(for: check)
                                }
                                .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.steel))
                            }
                        }
                    }
                }
            }
        }
    }

    private var capabilityGroupsCard: some View {
        NexusCard("Capability Groups", eyebrow: "How This Mac Is Modeled") {
            if let capabilities = model.snapshot?.nodeCard?.capabilities, !capabilities.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(capabilities) { capability in
                        dashboardItemCard {
                            HStack {
                                Text(CapabilityPresentation.title(for: capability.capabilityID))
                                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                                    .foregroundStyle(NexusPalette.textPrimary)
                                Spacer()
                                NexusBadge(text: "\(capability.tools.count) tools", tone: NexusPalette.ocean)
                            }
                            Text(CapabilityPresentation.summary(
                                description: capability.description,
                                tools: capability.tools
                            ))
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                            Text(capability.tools.joined(separator: " · "))
                                .font(.system(size: 11, weight: .medium, design: .rounded))
                                .foregroundStyle(NexusPalette.textSecondary.opacity(0.7))
                        }
                    }
                }
            } else {
                Text("Capability groups will appear after the local engine publishes the current node card.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
        }
    }

    private var toolsCard: some View {
        NexusCard("Available Tools", eyebrow: "What This Mac Can Do") {
            if let snapshot = model.snapshot, !snapshot.tools.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(snapshot.tools) { tool in
                        dashboardItemCard {
                            HStack {
                                Text(tool.name)
                                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                                Spacer()
                                NexusBadge(
                                    text: tool.requiresApproval ? "approval" : tool.riskLevel,
                                    tone: tool.requiresApproval ? NexusPalette.rose : NexusPalette.cyan
                                )
                            }
                            Text(tool.description)
                                .font(.system(size: 12, weight: .medium, design: .rounded))
                                .foregroundStyle(NexusPalette.textSecondary)
                            Text(tool.tags.joined(separator: " · "))
                                .font(.system(size: 11, weight: .medium, design: .rounded))
                                .foregroundStyle(NexusPalette.textSecondary.opacity(0.7))
                        }
                    }
                }
            } else {
                Text("No tool metadata available yet.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
        }
    }

    private var eventsCard: some View {
        NexusCard("Recent Activity", eyebrow: "What Just Happened") {
            if let snapshot = model.snapshot, !snapshot.recentEvents.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(snapshot.recentEvents.prefix(8)) { event in
                        dashboardItemCard {
                            HStack {
                                Text(event.message)
                                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                                    .foregroundStyle(NexusPalette.textPrimary)
                                Spacer()
                                NexusBadge(text: event.level, tone: eventTone(event.level))
                            }
                            Text("\(event.kind) · \(Date(timeIntervalSince1970: event.timestamp).formatted())")
                                .font(.system(size: 11, weight: .medium, design: .rounded))
                                .foregroundStyle(NexusPalette.textSecondary)
                            if !event.details.isEmpty {
                                Text(event.details.map { "\($0.key)=\($0.value.description)" }.sorted().joined(separator: " · "))
                                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                                    .foregroundStyle(NexusPalette.textSecondary.opacity(0.72))
                            }
                        }
                    }
                }
            } else {
                Text("No recent events.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
        }
    }

    private var logsCard: some View {
        NexusCard("Local Engine Logs", eyebrow: "Diagnostics") {
            if model.recentLogs.isEmpty {
                Text("No logs captured yet.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            } else {
                Text(model.recentLogs.suffix(24).joined(separator: "\n"))
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundStyle(NexusPalette.textPrimary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .background(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .fill(.black.opacity(0.3))
                    )
            }
        }
    }

    private var heroSummary: String {
        guard let snapshot = model.snapshot else {
            return "Nexus is waiting for the local engine to start. Once it is up, this Mac can offer browser, file, screen, and clipboard access to your Ubuntu hub."
        }
        switch snapshot.hub.connectivityState {
        case .connected:
            return "This Mac is fully connected to Ubuntu Hub, can expose \(snapshot.tools.count) tool(s), and currently has \(snapshot.pendingApprovals.count) approval gate(s)."
        case .brokerOnly:
            return "This Mac is still on the mesh broker, but Ubuntu Hub itself is not fully ready. Local execution continues while shared dispatch waits."
        case .reconnecting:
            return "This Mac is reconnecting to Ubuntu Hub. Local execution can continue, and shared work will resume after reconnection."
        case .localOnly:
            return "This Mac is currently operating in local-only mode. It can still use local tools, but Hub-dependent work will wait until Ubuntu Hub returns."
        }
    }

    private var statusBadge: some View {
        Group {
            if let snapshot = model.snapshot {
                if !snapshot.pendingApprovals.isEmpty {
                    NexusBadge(text: "Needs approval", tone: NexusPalette.rose)
                } else {
                    switch snapshot.hub.connectivityState {
                    case .connected:
                        NexusBadge(text: "Connected", tone: NexusPalette.mint)
                    case .brokerOnly:
                        NexusBadge(text: "Broker only", tone: NexusPalette.amber)
                    case .reconnecting:
                        NexusBadge(text: "Reconnecting", tone: NexusPalette.ocean)
                    case .localOnly:
                        NexusBadge(text: "Local only", tone: NexusPalette.amber)
                    }
                }
            } else {
                NexusBadge(text: "Starting", tone: NexusPalette.steel)
            }
        }
    }

    private func eventTone(_ level: String) -> Color {
        switch level.lowercased() {
        case "error":
            return NexusPalette.rose
        case "warning":
            return NexusPalette.amber
        default:
            return NexusPalette.cyan
        }
    }

    private func dashboardItemCard<Content: View>(
        tone: Color = NexusPalette.steel.opacity(0.08),
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(tone)
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .stroke(NexusPalette.steel.opacity(0.16), lineWidth: 1)
                )
        )
    }
}
