import SwiftUI

struct MenuBarContentView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var windows: WindowCoordinator

    var body: some View {
        ZStack {
            NexusPanelBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    hero
                    openWorkspaceCard
                    mlxServiceCard
                    if let snapshot = model.snapshot, !snapshot.pendingApprovals.isEmpty {
                        approvalsPreview(snapshot.pendingApprovals)
                    }
                    quickActions
                    if let error = model.lastError, !error.isEmpty {
                        errorCard(error)
                    }
                    Button("Quit Nexus") {
                        model.quitApplication()
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.steel))
                }
                .padding(18)
            }
            .scrollIndicators(.hidden)
        }
        .frame(width: 430, height: 400)
    }

    // MARK: - Hero (status)

    private var hero: some View {
        HStack(spacing: 0) {
            Text("Nexus")
                .font(.system(size: 18, weight: .bold, design: .rounded))
                .foregroundStyle(NexusPalette.textPrimary)

            Spacer()

            statusDot(
                label: "Local",
                on: model.snapshot?.phase == "running",
                onColor: NexusPalette.mint
            )

            statusDot(
                label: "Hub",
                on: model.hubConnectivityState == .connected,
                onColor: NexusPalette.cyan
            )

            statusDot(
                label: "MLX",
                on: model.mlxHealth != nil || model.mlxSupervisorState != .stopped,
                onColor: model.mlxStatusTone
            )

            if model.pendingApprovalCount > 0 {
                NexusBadge(text: "\(model.pendingApprovalCount)", tone: NexusPalette.rose)
                    .padding(.leading, 8)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color.white.opacity(0.04))
                .overlay(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .stroke(Color.white.opacity(0.08), lineWidth: 1)
                )
        )
    }

    private func statusDot(label: String, on: Bool, onColor: Color) -> some View {
        HStack(spacing: 5) {
            Circle()
                .fill(on ? onColor : NexusPalette.steel.opacity(0.5))
                .frame(width: 7, height: 7)
            Text(label)
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(on ? NexusPalette.textPrimary : NexusPalette.textSecondary)
        }
        .padding(.leading, 14)
    }

    // MARK: - Open Workspace

    private var openWorkspaceCard: some View {
        Button {
            windows.showWorkspace(model: model)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "macwindow.on.rectangle")
                    .font(.system(size: 22, weight: .medium))
                    .foregroundStyle(NexusPalette.mint)

                VStack(alignment: .leading, spacing: 2) {
                    Text("Open Workspace")
                        .font(.system(size: 16, weight: .bold, design: .rounded))
                        .foregroundStyle(NexusPalette.textPrimary)
                    Text("Documents, AI Chat, and Task Progress")
                        .font(.system(size: 12, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                }

                Spacer()

                Image(systemName: "arrow.up.right")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
            .padding(16)
            .background(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(
                        LinearGradient(
                            colors: [
                                NexusPalette.mint.opacity(0.15),
                                NexusPalette.cyan.opacity(0.08)
                            ],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .stroke(NexusPalette.mint.opacity(0.25), lineWidth: 1)
                    )
            )
        }
        .buttonStyle(.plain)
    }

    // MARK: - MLX Service

    private var mlxServiceCard: some View {
        NexusCard("Local MLX", eyebrow: "Inference") {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.mlxStatusLabel)
                            .font(.system(size: 15, weight: .bold, design: .rounded))
                            .foregroundStyle(NexusPalette.textPrimary)
                        Text(model.mlxStatusDetail)
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                    }

                    Spacer()

                    NexusBadge(text: model.mlxStatusBadgeText, tone: model.mlxStatusTone)
                }

                if let health = model.mlxHealth {
                    VStack(alignment: .leading, spacing: 8) {
                        NexusInlineStatusRow(label: "Model", value: health.model, tone: NexusPalette.cyan)
                        NexusInlineStatusRow(label: "Loaded", value: health.loaded ? "Yes" : "No", tone: health.loaded ? NexusPalette.mint : NexusPalette.amber)
                        if let device = health.device {
                            NexusInlineStatusRow(label: "Device", value: device, tone: NexusPalette.mint)
                        }
                        if let cacheDir = health.cacheDir {
                            NexusInlineStatusRow(label: "Cache", value: cacheDir, tone: NexusPalette.steel)
                        }
                    }
                } else {
                    VStack(alignment: .leading, spacing: 8) {
                        NexusInlineStatusRow(label: "Endpoint", value: model.mlxEndpointLabel, tone: NexusPalette.steel)
                        NexusInlineStatusRow(label: "Script", value: model.mlxServeScriptPath, tone: NexusPalette.steel)
                    }
                }

                HStack(spacing: 10) {
                    Button("Start MLX") {
                        model.startMLXService()
                    }
                    .buttonStyle(NexusPrimaryButtonStyle())
                    .disabled(!model.canStartMLXService)

                    Button("Stop MLX") {
                        model.stopMLXService()
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.rose))
                    .disabled(!model.canStopMLXService)

                    Button("Refresh") {
                        Task { await model.refreshMLXServiceStatus() }
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.amber))
                }

                if let error = model.mlxLastError, !error.isEmpty {
                    Text(error)
                        .font(.system(size: 12, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.rose)
                }
            }
        }
    }

    // MARK: - Quick Actions

    private var quickActions: some View {
        NexusCard("Quick Actions") {
            VStack(spacing: 10) {
                HStack(spacing: 10) {
                    Button("Dashboard") { windows.showDashboard(model: model) }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.mint))
                    Button("Mesh Trace") { windows.showMeshTrace(model: model) }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.ocean))
                    Button("Task Log") { windows.showTaskLog(model: model) }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.purple))
                }
                HStack(spacing: 10) {
                    Button("Settings") { windows.showSettings(model: model) }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.steel))
                    Button("Refresh") {
                        Task {
                            await model.refreshStatus()
                            await model.refreshHubHealth()
                        }
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.amber))
                    Button("Restart") {
                        model.restartSidecar()
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.ocean))
                }
            }
        }
    }

    // MARK: - Approvals

    private func approvalsPreview(_ approvals: [PendingApprovalSnapshot]) -> some View {
        NexusCard("Waiting For You", eyebrow: "Approval Queue") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(approvals.prefix(2)) { approval in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text(approval.toolName)
                                .font(.system(size: 14, weight: .semibold, design: .rounded))
                                .foregroundStyle(NexusPalette.textPrimary)
                            Spacer()
                            NexusBadge(text: approval.riskLevel, tone: NexusPalette.rose)
                        }
                        Text(approval.reason)
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)

                        HStack(spacing: 10) {
                            Button("Approve") {
                                Task { await model.approveApproval(id: approval.id) }
                            }
                            .buttonStyle(NexusPrimaryButtonStyle())
                            Button("Reject") {
                                Task { await model.rejectApproval(id: approval.id, comment: "Rejected from command panel") }
                            }
                            .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.rose))
                        }
                    }
                    .padding(12)
                    .background(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .fill(NexusPalette.rose.opacity(0.09))
                    )
                }
                Button("Review In Dashboard") { windows.showDashboard(model: model) }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.rose))
            }
        }
    }

    private func errorCard(_ error: String) -> some View {
        NexusCard("Latest Error", eyebrow: "Diagnostics") {
            Text(error)
                .font(.system(size: 12, weight: .medium, design: .rounded))
                .foregroundStyle(NexusPalette.rose)
        }
    }
}
