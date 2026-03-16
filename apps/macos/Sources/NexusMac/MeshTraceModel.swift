import Foundation

struct MeshTraceEntry: Identifiable, Equatable {
    enum Lane: String, CaseIterable {
        case command
        case hub
        case node
        case engine
        case approval
        case error

        var label: String {
            switch self {
            case .command:
                return "Command"
            case .hub:
                return "Hub"
            case .node:
                return "Node"
            case .engine:
                return "Engine"
            case .approval:
                return "Approval"
            case .error:
                return "Error"
            }
        }
    }

    let id: UUID
    let timestamp: Date
    let lane: Lane
    let title: String
    let detail: String
    let metadata: String?
    let sessionID: String?

    init(
        id: UUID = UUID(),
        timestamp: Date = .now,
        lane: Lane,
        title: String,
        detail: String,
        metadata: String? = nil,
        sessionID: String? = nil
    ) {
        self.id = id
        self.timestamp = timestamp
        self.lane = lane
        self.title = title
        self.detail = detail
        self.metadata = metadata
        self.sessionID = sessionID
    }
}
