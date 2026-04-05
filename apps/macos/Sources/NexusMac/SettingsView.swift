import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: AppModel

    private let preferredWidth: CGFloat = 960
    private let preferredHeight: CGFloat = 860
    private let minimumWidth: CGFloat = 900
    private let minimumHeight: CGFloat = 780

    var body: some View {
        ZStack {
            NexusPanelBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    header
                    workspaceCard
                    runtimeCard
                    permissionsCard
                    launchAgentCard
                    actionsCard
                }
                .padding(24)
                .frame(maxWidth: preferredWidth, alignment: .leading)
            }
            .scrollIndicators(.hidden)
        }
        .frame(
            minWidth: minimumWidth,
            idealWidth: preferredWidth,
            minHeight: minimumHeight,
            idealHeight: preferredHeight,
            alignment: .topLeading
        )
    }

    private var header: some View {
        NexusCard("Preferences", eyebrow: "Nexus") {
            Text("Choose where the local engine runs, how this Mac reaches your Ubuntu hub, and whether Nexus should launch automatically.")
                .font(.system(size: 14, weight: .medium, design: .rounded))
                .foregroundStyle(NexusPalette.textSecondary)
        }
    }

    private var workspaceCard: some View {
        NexusCard("Workspace", eyebrow: "Paths") {
            VStack(spacing: 14) {
                NexusField(title: "Nexus Root", text: stringBinding(\.nexusRoot))
                NexusField(title: "Node Card", text: stringBinding(\.nodeCardPath))
                NexusField(title: "Conda Executable", text: stringBinding(\.condaExecutable))
            }
        }
    }

    private var runtimeCard: some View {
        NexusCard("Runtime", eyebrow: "Connection & Local Engine") {
            VStack(spacing: 16) {
                HStack(spacing: 14) {
                    NexusField(title: "Local Host", text: stringBinding(\.localHost))
                    NexusField(title: "Local Port", text: intBinding(\.localPort))
                }
                HStack(spacing: 14) {
                    NexusField(title: "Broker Host", text: stringBinding(\.brokerHost))
                    NexusField(title: "Broker Port", text: intBinding(\.brokerPort))
                }
                HStack(spacing: 14) {
                    NexusField(title: "Hub API Host", text: stringBinding(\.hubAPIHost))
                    NexusField(title: "Hub API Port", text: intBinding(\.hubAPIPort))
                }
                NexusField(title: "Hub Bearer Token", text: stringBinding(\.hubBearerToken))
                if let snapshot = model.snapshot {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("LIVE HUB STATE")
                            .font(.system(size: 11, weight: .bold, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                        Text("\(snapshot.hub.connectivityState.label) · \(snapshot.hub.description)")
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                    }
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("TRANSPORT")
                        .font(.system(size: 11, weight: .bold, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                    Picker("Transport", selection: stringBinding(\.meshTransport)) {
                        Text("tcp").tag("tcp")
                        Text("websockets").tag("websockets")
                    }
                    .pickerStyle(.segmented)
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("COMMAND MODE")
                        .font(.system(size: 11, weight: .bold, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                    Picker("Command Mode", selection: Binding(
                        get: { model.settings.commandMode },
                        set: {
                            model.settings.commandMode = $0
                            model.persistSettings()
                        }
                    )) {
                        ForEach(CommandMode.allCases, id: \.self) { mode in
                            Text(mode.label).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    Text(model.settings.commandMode.description)
                        .font(.system(size: 12, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                }
                Toggle(isOn: boolBinding(\.autoStartSidecar)) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Start local engine when Nexus opens")
                            .font(.system(size: 14, weight: .semibold, design: .rounded))
                            .foregroundStyle(NexusPalette.textPrimary)
                        Text("Keeps this Mac ready to serve browser, file, screen, and clipboard tasks.")
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(NexusPalette.textSecondary)
                    }
                }
                .toggleStyle(.switch)
            }
        }
    }

    private var launchAgentCard: some View {
        NexusCard("LaunchAgent", eyebrow: "Startup") {
            VStack(alignment: .leading, spacing: 12) {
                if let status = model.launchAgentStatus {
                    NexusKeyValueRow(key: "Installed", value: status.installed ? "Yes" : "No")
                    NexusKeyValueRow(key: "Loaded", value: status.loaded ? "Yes" : "No")
                    NexusKeyValueRow(key: "Plist", value: status.plistPath)
                    if let executablePath = status.executablePath {
                        NexusKeyValueRow(key: "Executable", value: executablePath)
                    }
                } else {
                    Text("LaunchAgent status unavailable.")
                        .font(.system(size: 13, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                }

                HStack(spacing: 10) {
                    Button("Install / Update") {
                        Task { await model.installLaunchAgent() }
                    }
                    .buttonStyle(NexusPrimaryButtonStyle())
                    Button("Remove") {
                        Task { await model.removeLaunchAgent() }
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.rose))
                    Button("Refresh") {
                        Task { await model.refreshLaunchAgentStatus() }
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.steel))
                }
                Text("This LaunchAgent is for launch-at-login, not forced keep-alive. Quitting Nexus should stop it for the current session without removing the plist; next login will launch it again. For the current development stage, it points at the current built executable.")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            }
        }
    }

    private var permissionsCard: some View {
        NexusCard("Permissions", eyebrow: "macOS Access") {
            VStack(alignment: .leading, spacing: 12) {
                Text("Nexus currently uses a local Python sidecar plus macOS system automation commands. These checks show whether the current app session can request or use the main protected resources.")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)

                ForEach(model.permissionChecks) { check in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .center, spacing: 10) {
                            Text(check.title)
                                .font(.system(size: 14, weight: .semibold, design: .rounded))
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
                    .padding(14)
                    .background(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .fill(.thinMaterial)
                            .environment(\.colorScheme, .dark)
                            .overlay(
                                RoundedRectangle(cornerRadius: 16, style: .continuous)
                                    .stroke(PermissionDiagnostics.stateTone(check.state).opacity(0.35), lineWidth: 1)
                            )
                    )
                }
            }
        }
    }

    private var actionsCard: some View {
        NexusCard("Actions", eyebrow: "Control") {
            HStack(spacing: 10) {
                Button("Restart Local Engine") {
                    model.restartSidecar()
                }
                .buttonStyle(NexusPrimaryButtonStyle())
                Button("Refresh Permissions") {
                    Task { await model.refreshPermissionChecks() }
                }
                .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.amber))
                Button("Save Settings") {
                    model.persistSettings()
                }
                .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.ocean))
            }
        }
    }

    private func stringBinding(_ keyPath: WritableKeyPath<SidecarSettings, String>) -> Binding<String> {
        Binding(
            get: { model.settings[keyPath: keyPath] },
            set: {
                model.settings[keyPath: keyPath] = $0
                model.persistSettings()
            }
        )
    }

    private func boolBinding(_ keyPath: WritableKeyPath<SidecarSettings, Bool>) -> Binding<Bool> {
        Binding(
            get: { model.settings[keyPath: keyPath] },
            set: {
                model.settings[keyPath: keyPath] = $0
                model.persistSettings()
            }
        )
    }

    private func intBinding(_ keyPath: WritableKeyPath<SidecarSettings, Int>) -> Binding<String> {
        Binding(
            get: { String(model.settings[keyPath: keyPath]) },
            set: {
                if let value = Int($0) {
                    model.settings[keyPath: keyPath] = value
                    model.persistSettings()
                }
            }
        )
    }
}
