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
                    commandPanel
                    if let snapshot = model.snapshot, !snapshot.pendingApprovals.isEmpty {
                        approvalsPreview(snapshot.pendingApprovals)
                    }
                    conversationPreview
                    secondaryActions
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
        .frame(width: 430, height: 760)
    }

    private var hero: some View {
        NexusCard("Command Panel", eyebrow: "Nexus") {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(model.statusLabel)
                            .font(.system(size: 24, weight: .bold, design: .rounded))
                            .foregroundStyle(NexusPalette.textPrimary)
                        Text(heroSummary)
                            .font(.system(size: 13, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    heroStatusCluster
                }

                VStack(alignment: .leading, spacing: 8) {
                    NexusInlineStatusRow(
                        label: "Local Engine",
                        value: model.snapshot?.phase.capitalized ?? model.statusLabel,
                        tone: model.snapshot?.phase == "running" ? NexusPalette.mint : NexusPalette.amber
                    )
                    NexusInlineStatusRow(
                        label: "Ubuntu Hub",
                        value: "\(model.primaryHubStatusLabel) · \(model.settings.resolvedHubAPIHost):\(model.settings.resolvedHubAPIPort)",
                        tone: hubTone
                    )
                }
            }
        }
    }

    private var commandPanel: some View {
        NexusCard("Ask Nexus", eyebrow: "Primary Interaction") {
            VStack(alignment: .leading, spacing: 12) {
                Text(model.commandModeDescription)
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)

                Picker("Mode", selection: $model.settings.commandMode) {
                    ForEach(CommandMode.allCases, id: \.self) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: model.settings.commandMode) { _ in
                    model.persistSettings()
                }

                NexusTextComposer(
                    text: $model.commandDraft,
                    placeholder: commandPlaceholder
                )

                HStack(spacing: 10) {
                    Button(model.sendButtonLabel) {
                        model.sendCommand()
                    }
                    .buttonStyle(NexusPrimaryButtonStyle())
                    .disabled(!model.canSendCommand)

                    Button("Clear") {
                        model.clearConversation()
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.steel))
                }
            }
        }
    }

    private var commandPlaceholder: String {
        switch model.settings.commandMode {
        case .hub:
            return "Describe what you want. The Hub will plan and coordinate across nodes."
        case .local:
            return "Describe what you want. This Mac will execute using its local tools and API LLM."
        case .auto:
            return "Describe what you want. Nexus will try the Hub first, then fall back to this Mac."
        }
    }

    private var conversationPreview: some View {
        NexusCard("Recent Conversation", eyebrow: "Replies") {
            if model.conversation.isEmpty {
                Text("Your message flow will appear here. This is now the main entry point; Dashboard and Settings are secondary.")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(model.conversation.suffix(6)) { entry in
                        NexusConversationBubble(entry: entry)
                    }
                }
            }
        }
    }

    private var secondaryActions: some View {
        NexusCard("Secondary Views", eyebrow: "Support Surfaces") {
            VStack(spacing: 10) {
                HStack(spacing: 10) {
                    Button("Open Dashboard") { windows.showDashboard(model: model) }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.mint))
                    Button("Open Mesh Trace") { windows.showMeshTrace(model: model) }
                        .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.ocean))
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
                    Button("Restart Engine") {
                        model.restartSidecar()
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.ocean))
                }
            }
        }
    }

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

    private var heroStatusCluster: some View {
        VStack(alignment: .trailing, spacing: 8) {
            if model.pendingApprovalCount > 0 {
                NexusBadge(text: "\(model.pendingApprovalCount) approvals", tone: NexusPalette.rose)
            }
            if model.snapshot?.transportConnected == true {
                NexusBadge(text: "Mac node online", tone: NexusPalette.mint)
            } else {
                NexusBadge(text: "Mac local only", tone: NexusPalette.amber)
            }
            NexusBadge(text: hubBadgeText, tone: hubTone)
        }
    }

    private var heroSummary: String {
        switch model.hubConnectivityState {
        case .connected:
            return "Use this panel to talk to Nexus. Ubuntu Hub will plan the task, and this Mac will execute the steps that need local browser, file, screen, or automation access."
        case .brokerOnly:
            return "This Mac still has mesh transport, but Ubuntu Hub itself is not fully ready. Local commands can still run here."
        case .reconnecting:
            return "This Mac is reconnecting to Ubuntu Hub. Local work can continue, and Hub work will resume when the control plane returns."
        case .localOnly, .none:
            return "The local engine is running on this Mac, but Ubuntu Hub is unavailable. This Mac can still do local work and will wait for Hub to come back for shared tasks."
        }
    }

    private var hubBadgeText: String {
        model.hubConnectivityState?.label ?? "Hub unknown"
    }

    private var hubTone: Color {
        switch model.hubConnectivityState {
        case .connected:
            return NexusPalette.cyan
        case .brokerOnly:
            return NexusPalette.amber
        case .reconnecting:
            return NexusPalette.ocean
        case .localOnly, .none:
            return NexusPalette.steel
        }
    }
}
