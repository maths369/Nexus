import SwiftUI

struct MeshTraceView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ZStack {
            NexusPanelBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    metrics
                    timeline
                }
                .padding(28)
                .frame(maxWidth: 1240, alignment: .center)
            }
            .scrollIndicators(.hidden)
        }
    }

    private var header: some View {
        NexusCard("Mesh Trace", eyebrow: "Investigation") {
            HStack(alignment: .top, spacing: 18) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Watch what the Hub planned, what the Mac node received, and what the local engine actually did.")
                        .font(.system(size: 14, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)

                    VStack(alignment: .leading, spacing: 8) {
                        NexusInlineStatusRow(
                            label: "Hub",
                            value: "\(model.primaryHubStatusLabel) · \(model.settings.resolvedHubAPIHost):\(model.settings.resolvedHubAPIPort)",
                            tone: hubTone
                        )
                        NexusInlineStatusRow(
                            label: "Mac Node",
                            value: model.snapshot?.transportConnected == true ? "Connected to broker" : "Not connected to broker",
                            tone: model.snapshot?.transportConnected == true ? NexusPalette.mint : NexusPalette.rose
                        )
                        NexusInlineStatusRow(
                            label: "Trace",
                            value: "\(model.meshTrace.count) entries in memory",
                            tone: NexusPalette.steel
                        )
                    }
                }
                Spacer()
                VStack(spacing: 10) {
                    Button("Refresh Now") {
                        Task {
                            await model.refreshStatus()
                            await model.refreshHubHealth()
                        }
                    }
                    .buttonStyle(NexusPrimaryButtonStyle())

                    Button("Clear Trace Buffer") {
                        model.clearMeshTrace()
                    }
                    .buttonStyle(NexusSecondaryButtonStyle(tone: NexusPalette.steel))
                }
                .frame(maxWidth: 220)
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
                    TraceMetricTile(label: "Hub", count: model.meshTraceCount(for: .hub), tone: NexusPalette.cyan)
                    TraceMetricTile(label: "Node", count: model.meshTraceCount(for: .node), tone: NexusPalette.mint)
                }
                HStack(spacing: 14) {
                    TraceMetricTile(label: "Engine", count: model.meshTraceCount(for: .engine), tone: NexusPalette.amber)
                    TraceMetricTile(label: "Errors", count: model.meshTraceCount(for: .error), tone: NexusPalette.rose)
                }
            }
        }
    }

    @ViewBuilder
    private var metricTiles: some View {
        TraceMetricTile(label: "Hub", count: model.meshTraceCount(for: .hub), tone: NexusPalette.cyan)
        TraceMetricTile(label: "Node", count: model.meshTraceCount(for: .node), tone: NexusPalette.mint)
        TraceMetricTile(label: "Engine", count: model.meshTraceCount(for: .engine), tone: NexusPalette.amber)
        TraceMetricTile(label: "Errors", count: model.meshTraceCount(for: .error), tone: NexusPalette.rose)
    }

    private var timeline: some View {
        NexusCard("Chronological Timeline", eyebrow: "Hub, Node, And Local Engine") {
            if model.meshTrace.isEmpty {
                Text("No trace entries yet. Send a command or refresh the local engine, and Nexus will record Hub messages, node events, and sidecar logs here.")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textSecondary)
            } else {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(model.meshTrace.reversed())) { entry in
                        MeshTraceRow(entry: entry)
                    }
                }
            }
        }
    }
}

private extension MeshTraceView {
    var hubTone: Color {
        switch model.hubConnectivityState {
        case .connected:
            return NexusPalette.cyan
        case .brokerOnly:
            return NexusPalette.amber
        case .reconnecting:
            return NexusPalette.ocean
        case .localOnly, .none:
            return NexusPalette.rose
        }
    }
}

private struct TraceMetricTile: View {
    let label: String
    let count: Int
    let tone: Color

    var body: some View {
        NexusMetricTile(label: label, value: String(count), tone: tone)
    }
}

private struct MeshTraceRow: View {
    let entry: MeshTraceEntry

    private static let formatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        return formatter
    }()

    private var tone: Color {
        switch entry.lane {
        case .command:
            return NexusPalette.ocean
        case .hub:
            return NexusPalette.cyan
        case .node:
            return NexusPalette.mint
        case .engine:
            return NexusPalette.amber
        case .approval:
            return NexusPalette.purple
        case .error:
            return NexusPalette.rose
        }
    }

    private var summaryText: String {
        let flattenedDetail = entry.detail
            .replacingOccurrences(of: "\n", with: " · ")
            .replacingOccurrences(of: "  ", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let pieces = [
            flattenedDetail,
            entry.metadata?.trimmingCharacters(in: .whitespacesAndNewlines),
            entry.sessionID.map { "session=\($0)" }
        ]
        return pieces
            .compactMap { value in
                guard let value, !value.isEmpty else { return nil }
                return value
            }
            .joined(separator: " · ")
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            NexusBadge(text: entry.lane.label, tone: tone)
                .frame(width: 82, alignment: .leading)

            Text(Self.formatter.string(from: entry.timestamp))
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(NexusPalette.textTertiary)
                .frame(width: 96, alignment: .leading)

            Text(entry.title)
                .font(.system(size: 13, weight: .semibold, design: .rounded))
                .foregroundStyle(NexusPalette.textPrimary)
                .frame(width: 190, alignment: .leading)
                .lineLimit(1)

            Text(summaryText)
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .foregroundStyle(NexusPalette.textSecondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(tone.opacity(0.08))
                .overlay(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .stroke(tone.opacity(0.2), lineWidth: 1)
                )
        )
    }
}

private extension AppModel {
    func meshTraceCount(for lane: MeshTraceEntry.Lane) -> Int {
        meshTrace.filter { $0.lane == lane }.count
    }
}
