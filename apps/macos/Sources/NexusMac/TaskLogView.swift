import SwiftUI

// MARK: - Data Model

struct TaskLogEntryData: Identifiable, Codable {
    let task_id: String
    let timestamp: Double
    let phase: String
    let source: String
    let node: String
    let message: String
    let details: [String: AnyCodable]?

    var id: String { "\(task_id)-\(timestamp)" }

    var date: Date { Date(timeIntervalSince1970: timestamp) }

    var phaseIcon: String {
        switch phase {
        case "received": return "arrow.down.circle.fill"
        case "planned": return "list.bullet.clipboard.fill"
        case "dispatched": return "paperplane.fill"
        case "executing": return "gearshape.2.fill"
        case "tool_call": return "wrench.fill"
        case "tool_result": return "checkmark.circle.fill"
        case "completed": return "checkmark.seal.fill"
        case "failed": return "xmark.octagon.fill"
        default: return "circle.fill"
        }
    }

    var phaseColor: Color {
        switch phase {
        case "received": return NexusPalette.cyan
        case "planned": return NexusPalette.purple
        case "dispatched": return NexusPalette.amber
        case "executing": return NexusPalette.mint
        case "tool_call": return NexusPalette.steel
        case "tool_result": return NexusPalette.mint
        case "completed": return NexusPalette.mint
        case "failed": return NexusPalette.rose
        default: return NexusPalette.steel
        }
    }

    var sourceLabel: String {
        switch source {
        case "feishu": return "Feishu"
        case "desktop": return "Desktop"
        case "hub": return "Hub"
        case "mqtt": return "MQTT"
        case "local": return "Local"
        default: return source
        }
    }
}

struct AnyCodable: Codable {
    let value: Any

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let s = try? container.decode(String.self) { value = s }
        else if let i = try? container.decode(Int.self) { value = i }
        else if let d = try? container.decode(Double.self) { value = d }
        else if let b = try? container.decode(Bool.self) { value = b }
        else { value = "" }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let s = value as? String { try container.encode(s) }
        else if let i = value as? Int { try container.encode(i) }
        else if let d = value as? Double { try container.encode(d) }
        else if let b = value as? Bool { try container.encode(b) }
        else { try container.encode(String(describing: value)) }
    }

    var description: String {
        if let s = value as? String { return s }
        return String(describing: value)
    }
}

struct TaskLogResponse: Codable {
    let entries: [TaskLogEntryData]
    let count: Int
}

// MARK: - ViewModel

@MainActor
class TaskLogModel: ObservableObject {
    @Published var entries: [TaskLogEntryData] = []
    @Published var isLoading = false
    @Published var lastError: String?
    @Published var autoRefresh = true

    private var refreshTimer: Timer?
    private let sidecarBaseURL: String

    init(sidecarBaseURL: String = "http://127.0.0.1:8765") {
        self.sidecarBaseURL = sidecarBaseURL
        startAutoRefresh()
    }

    func fetch() async {
        isLoading = true
        defer { isLoading = false }

        guard let url = URL(string: "\(sidecarBaseURL)/task-log?limit=200") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let response = try JSONDecoder().decode(TaskLogResponse.self, from: data)
            entries = response.entries
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func startAutoRefresh() {
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            guard let self, self.autoRefresh else { return }
            Task { @MainActor in
                await self.fetch()
            }
        }
        Task { await fetch() }
    }

    func clear() async {
        entries = []
    }

    nonisolated deinit {
        // Timer cleanup happens when the object is deallocated
    }

    /// Group entries by task_id, ordered by most recent task first
    var groupedByTask: [(taskId: String, entries: [TaskLogEntryData])] {
        let grouped = Dictionary(grouping: entries, by: { $0.task_id })
        return grouped
            .map { (taskId: $0.key, entries: $0.value.sorted { $0.timestamp < $1.timestamp }) }
            .sorted { ($0.entries.last?.timestamp ?? 0) > ($1.entries.last?.timestamp ?? 0) }
    }
}

// MARK: - View

struct TaskLogView: View {
    @ObservedObject var model: AppModel
    @StateObject private var logModel = TaskLogModel()

    var body: some View {
        ZStack {
            NexusPanelBackground()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    header
                    taskGroups
                }
                .padding(28)
                .frame(maxWidth: 1240, alignment: .center)
            }
            .scrollIndicators(.hidden)
        }
    }

    private var header: some View {
        NexusCard("Task Log", eyebrow: "Observability") {
            HStack(alignment: .top, spacing: 18) {
                VStack(alignment: .leading, spacing: 10) {
                    Text("View all tasks received, dispatched, and executed across mesh nodes.")
                        .font(.system(size: 14, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)

                    HStack(spacing: 16) {
                        NexusInlineStatusRow(
                            label: "Total Entries",
                            value: "\(logModel.entries.count)",
                            tone: NexusPalette.cyan
                        )
                        NexusInlineStatusRow(
                            label: "Tasks",
                            value: "\(logModel.groupedByTask.count)",
                            tone: NexusPalette.mint
                        )
                        if let err = logModel.lastError {
                            NexusInlineStatusRow(
                                label: "Error",
                                value: err,
                                tone: NexusPalette.rose
                            )
                        }
                    }
                }
                Spacer()
                VStack(spacing: 10) {
                    Button("Refresh") {
                        Task { await logModel.fetch() }
                    }
                    .buttonStyle(NexusPrimaryButtonStyle())

                    Toggle("Auto (3s)", isOn: $logModel.autoRefresh)
                        .toggleStyle(.switch)
                        .font(.system(size: 12, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                }
                .frame(maxWidth: 160)
            }
        }
    }

    private var taskGroups: some View {
        VStack(alignment: .leading, spacing: 14) {
            if logModel.groupedByTask.isEmpty {
                NexusCard("No Tasks", eyebrow: "Empty") {
                    Text("No task log entries yet. Send a command to see activity here.")
                        .font(.system(size: 14, weight: .medium, design: .rounded))
                        .foregroundStyle(NexusPalette.textSecondary)
                }
            } else {
                ForEach(logModel.groupedByTask, id: \.taskId) { group in
                    TaskGroupCard(taskId: group.taskId, entries: group.entries)
                }
            }
        }
    }
}

// MARK: - Task Group Card

private struct TaskGroupCard: View {
    let taskId: String
    let entries: [TaskLogEntryData]
    @State private var isExpanded = true

    private var latestPhase: String {
        entries.last?.phase ?? "unknown"
    }

    private var taskTitle: String {
        // Find the "received" entry for the task description
        if let received = entries.first(where: { $0.phase == "received" }) {
            return received.message
        }
        return entries.first?.message ?? taskId
    }

    private var statusColor: Color {
        entries.last?.phaseColor ?? NexusPalette.steel
    }

    private var durationLabel: String? {
        guard let first = entries.first, let last = entries.last else { return nil }
        let duration = last.timestamp - first.timestamp
        if duration < 1 { return "<1s" }
        if duration < 60 { return String(format: "%.0fs", duration) }
        return String(format: "%.1fmin", duration / 60)
    }

    var body: some View {
        NexusCard(taskId, eyebrow: "Task") {
            VStack(alignment: .leading, spacing: 12) {
                // Task summary header
                HStack(spacing: 12) {
                    Image(systemName: entries.last?.phaseIcon ?? "circle.fill")
                        .foregroundStyle(statusColor)
                        .font(.system(size: 16))

                    VStack(alignment: .leading, spacing: 2) {
                        Text(taskTitle)
                            .font(.system(size: 13, weight: .semibold, design: .rounded))
                            .foregroundStyle(NexusPalette.textPrimary)
                            .lineLimit(2)

                        HStack(spacing: 8) {
                            Text(latestPhase.uppercased())
                                .font(.system(size: 10, weight: .bold, design: .monospaced))
                                .foregroundStyle(statusColor)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(statusColor.opacity(0.15))
                                .cornerRadius(4)

                            if let source = entries.first?.sourceLabel {
                                Text(source)
                                    .font(.system(size: 10, weight: .medium, design: .rounded))
                                    .foregroundStyle(NexusPalette.textSecondary)
                            }

                            if let node = entries.first?.node {
                                Text(node)
                                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                                    .foregroundStyle(NexusPalette.textSecondary)
                            }

                            if let dur = durationLabel {
                                Text(dur)
                                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                                    .foregroundStyle(NexusPalette.amber)
                            }
                        }
                    }

                    Spacer()

                    Button(action: { withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() } }) {
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .foregroundStyle(NexusPalette.textSecondary)
                    }
                    .buttonStyle(.plain)
                }

                // Timeline entries (expanded)
                if isExpanded {
                    Divider()
                        .background(NexusPalette.steel.opacity(0.3))

                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(entries) { entry in
                            TaskLogEntryRow(entry: entry)
                        }
                    }
                }
            }
        }
    }
}

// MARK: - Entry Row

private struct TaskLogEntryRow: View {
    let entry: TaskLogEntryData

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Timeline dot + line
            VStack(spacing: 0) {
                Circle()
                    .fill(entry.phaseColor)
                    .frame(width: 8, height: 8)
                Rectangle()
                    .fill(entry.phaseColor.opacity(0.2))
                    .frame(width: 2)
            }
            .frame(width: 8)

            // Content
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Image(systemName: entry.phaseIcon)
                        .font(.system(size: 11))
                        .foregroundStyle(entry.phaseColor)

                    Text(entry.phase.uppercased())
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundStyle(entry.phaseColor)

                    Spacer()

                    Text(Self.timeFormatter.string(from: entry.date))
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(NexusPalette.textSecondary)
                }

                Text(entry.message)
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)

                // Details
                if let details = entry.details, !details.isEmpty {
                    let detailKeys = ["output_preview", "error", "duration_ms", "tool_name", "step_id"]
                    let relevant = details.filter { detailKeys.contains($0.key) }
                    if !relevant.isEmpty {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(relevant.keys.sorted()), id: \.self) { key in
                                HStack(spacing: 4) {
                                    Text("\(key):")
                                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                        .foregroundStyle(NexusPalette.textSecondary)
                                    Text(relevant[key]?.description ?? "")
                                        .font(.system(size: 10, weight: .regular, design: .monospaced))
                                        .foregroundStyle(NexusPalette.textSecondary)
                                        .lineLimit(3)
                                }
                            }
                        }
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.black.opacity(0.15)))
                    }
                }
            }
            .padding(.vertical, 8)
        }
    }
}
