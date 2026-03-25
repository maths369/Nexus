import Foundation

enum JSONValue: Decodable, CustomStringConvertible, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value")
        }
    }

    var description: String {
        switch self {
        case let .string(value):
            return value
        case let .number(value):
            if value.rounded() == value {
                return String(Int(value))
            }
            return String(value)
        case let .bool(value):
            return value ? "true" : "false"
        case let .array(values):
            return values.map(\.description).joined(separator: ", ")
        case let .object(values):
            return values.map { "\($0.key)=\($0.value.description)" }.sorted().joined(separator: ", ")
        case .null:
            return "null"
        }
    }
}

struct SidecarHealthResponse: Decodable {
    let status: String
    let phase: String
    let transportConnected: Bool
    let hubConnectivityState: HubConnectivityState?
    let nodeID: String?

    enum CodingKeys: String, CodingKey {
        case status
        case phase
        case transportConnected = "transport_connected"
        case hubConnectivityState = "hub_connectivity_state"
        case nodeID = "node_id"
    }
}

enum HubConnectivityState: String, Codable {
    case connected = "connected"
    case brokerOnly = "broker_only"
    case localOnly = "local_only"
    case reconnecting = "reconnecting"

    var label: String {
        switch self {
        case .connected:
            return "Connected"
        case .brokerOnly:
            return "Broker only"
        case .localOnly:
            return "Local only"
        case .reconnecting:
            return "Reconnecting"
        }
    }

    var description: String {
        switch self {
        case .connected:
            return "Mesh broker, Hub API, and Hub runtime are all available."
        case .brokerOnly:
            return "This Mac is still on the broker, but the Hub API or registry is not fully ready."
        case .localOnly:
            return "This Mac can still execute local work, but Hub-dispatched work must wait."
        case .reconnecting:
            return "This Mac is retrying its Hub connection and will rejoin automatically."
        }
    }

    var canDispatchToHub: Bool {
        self == .connected
    }
}

struct PendingApprovalSnapshot: Decodable, Identifiable {
    var id: String { approvalID }
    let approvalID: String
    let requestedAt: Double
    let toolName: String
    let riskLevel: String
    let reason: String
    let arguments: [String: JSONValue]
    let source: String
    let taskID: String?
    let stepID: String?
    let sourceNode: String?
    let timeoutSeconds: Double?
    let comment: String?

    enum CodingKeys: String, CodingKey {
        case approvalID = "approval_id"
        case requestedAt = "requested_at"
        case toolName = "tool_name"
        case riskLevel = "risk_level"
        case reason
        case arguments
        case source
        case taskID = "task_id"
        case stepID = "step_id"
        case sourceNode = "source_node"
        case timeoutSeconds = "timeout_seconds"
        case comment
    }
}

struct SidecarStatusSnapshot: Decodable {
    struct HTTPInfo: Decodable {
        let host: String
        let port: Int
    }

    struct BrowserInfo: Decodable {
        let enabled: Bool
    }

    struct MeshInfo: Decodable {
        let brokerHost: String
        let brokerPort: Int
        let transport: String

        enum CodingKeys: String, CodingKey {
            case brokerHost = "broker_host"
            case brokerPort = "broker_port"
            case transport
        }
    }

    struct HubInfo: Decodable {
        let apiHost: String
        let apiPort: Int
        let apiHealthy: Bool
        let runtimeReady: Bool
        let hubNodeOnline: Bool
        let connectivityState: HubConnectivityState
        let reconnecting: Bool
        let lastCheckedAt: Double?
        let lastError: String?

        enum CodingKeys: String, CodingKey {
            case apiHost = "api_host"
            case apiPort = "api_port"
            case apiHealthy = "api_healthy"
            case runtimeReady = "runtime_ready"
            case hubNodeOnline = "hub_node_online"
            case connectivityState = "connectivity_state"
            case reconnecting
            case lastCheckedAt = "last_checked_at"
            case lastError = "last_error"
        }

        var description: String {
            connectivityState.description
        }
    }

    struct ResourceInfo: Decodable {
        let cpuCores: Int
        let memoryGB: Double
        let gpu: String
        let diskFreeGB: Double
        let batteryPowered: Bool

        enum CodingKeys: String, CodingKey {
            case cpuCores = "cpu_cores"
            case memoryGB = "memory_gb"
            case gpu
            case diskFreeGB = "disk_free_gb"
            case batteryPowered = "battery_powered"
        }
    }

    struct AvailabilityInfo: Decodable {
        let schedule: String
        let intermittent: Bool
        let maxTaskDurationSeconds: Int

        enum CodingKeys: String, CodingKey {
            case schedule
            case intermittent
            case maxTaskDurationSeconds = "max_task_duration_seconds"
        }
    }

    struct NodeCapability: Decodable, Identifiable {
        var id: String { capabilityID }
        let capabilityID: String
        let description: String
        let tools: [String]

        enum CodingKeys: String, CodingKey {
            case capabilityID = "capability_id"
            case description
            case tools
        }
    }

    struct NodeCardSnapshot: Decodable {
        let nodeID: String
        let displayName: String
        let platform: String
        let capabilities: [NodeCapability]
        let resources: ResourceInfo
        let availability: AvailabilityInfo

        enum CodingKeys: String, CodingKey {
            case nodeID = "node_id"
            case displayName = "display_name"
            case platform
            case capabilities
            case resources
            case availability
        }
    }

    struct ToolSnapshot: Decodable, Identifiable {
        var id: String { name }
        let name: String
        let description: String
        let riskLevel: String
        let requiresApproval: Bool
        let tags: [String]

        enum CodingKeys: String, CodingKey {
            case name
            case description
            case riskLevel = "risk_level"
            case requiresApproval = "requires_approval"
            case tags
        }
    }

    struct EventSnapshot: Decodable, Identifiable {
        var id: String { "\(timestamp)-\(kind)-\(message)" }
        let timestamp: Double
        let kind: String
        let level: String
        let message: String
        let details: [String: JSONValue]
    }

    let phase: String
    let transportConnected: Bool
    let rootDir: String
    let http: HTTPInfo
    let browser: BrowserInfo
    let mesh: MeshInfo
    let hub: HubInfo
    let nodeCard: NodeCardSnapshot?
    let tools: [ToolSnapshot]
    let activeExecutions: Int
    let startedAt: Double?
    let lastError: String?
    let pendingApprovals: [PendingApprovalSnapshot]
    let recentEvents: [EventSnapshot]

    enum CodingKeys: String, CodingKey {
        case phase
        case transportConnected = "transport_connected"
        case rootDir = "root_dir"
        case http
        case browser
        case mesh
        case hub
        case nodeCard = "node_card"
        case tools
        case activeExecutions = "active_executions"
        case startedAt = "started_at"
        case lastError = "last_error"
        case pendingApprovals = "pending_approvals"
        case recentEvents = "recent_events"
    }
}

struct SidecarApprovalsResponse: Decodable {
    let approvals: [PendingApprovalSnapshot]
}

struct SidecarApprovalMutationResponse: Decodable {
    let approval: PendingApprovalSnapshot
}

struct SidecarLaunchCommand: Equatable {
    let executable: String
    let arguments: [String]
    let currentDirectory: String
    var environment: [String: String] = [:]
}

enum CommandMode: String, Codable, CaseIterable {
    case auto = "auto"
    case hub = "hub"
    case local = "local"

    var label: String {
        switch self {
        case .auto: return "Auto"
        case .hub: return "Hub"
        case .local: return "This Mac"
        }
    }

    var description: String {
        switch self {
        case .auto: return "Try Hub first, fall back to this Mac"
        case .hub: return "Always send to Ubuntu Hub"
        case .local: return "Execute on this Mac using API LLM"
        }
    }
}

private struct ApprovalActionPayload: Encodable {
    let comment: String?
}

private struct LocalCommandPayload: Encodable {
    let task: String
    let systemPrompt: String?

    enum CodingKeys: String, CodingKey {
        case task
        case systemPrompt = "system_prompt"
    }
}

struct LocalCommandResult: Decodable {
    let runID: String
    let task: String
    let success: Bool
    let output: String
    let error: String?
    let durationMs: Double
    let model: String
    let eventCount: Int

    enum CodingKeys: String, CodingKey {
        case runID = "run_id"
        case task
        case success
        case output
        case error
        case durationMs = "duration_ms"
        case model
        case eventCount = "event_count"
    }
}

private struct LocalCommandResponse: Decodable {
    let result: LocalCommandResult
}

struct SidecarPermissionEntry: Decodable {
    let id: String
    let title: String
    let state: String
    let detail: String
}

struct SidecarPermissionsResponse: Decodable {
    let permissions: [SidecarPermissionEntry]
}

final class SidecarAPIClient {
    private let baseURL: URL
    private let session: URLSession

    init(host: String, port: Int, session: URLSession = .shared) {
        self.baseURL = URL(string: "http://\(host):\(port)")!
        self.session = session
    }

    func health() async throws -> SidecarHealthResponse {
        try await request(path: "health")
    }

    func status() async throws -> SidecarStatusSnapshot {
        try await request(path: "status")
    }

    func approvals() async throws -> SidecarApprovalsResponse {
        try await request(path: "approvals")
    }

    func approveApproval(id: String, comment: String? = nil) async throws -> SidecarApprovalMutationResponse {
        try await request(
            path: "approvals/\(id)/approve",
            method: "POST",
            body: ApprovalActionPayload(comment: comment)
        )
    }

    func rejectApproval(id: String, comment: String? = nil) async throws -> SidecarApprovalMutationResponse {
        try await request(
            path: "approvals/\(id)/reject",
            method: "POST",
            body: ApprovalActionPayload(comment: comment)
        )
    }

    func permissions() async throws -> SidecarPermissionsResponse {
        try await request(path: "permissions")
    }

    func localCommand(task: String, systemPrompt: String? = nil) async throws -> LocalCommandResult {
        let response: LocalCommandResponse = try await request(
            path: "local-command",
            method: "POST",
            body: LocalCommandPayload(task: task, systemPrompt: systemPrompt)
        )
        return response.result
    }

    private func request<T: Decodable>(path: String) async throws -> T {
        try await request(path: path, method: "GET", body: Optional<ApprovalActionPayload>.none)
    }

    private func request<T: Decodable, Body: Encodable>(path: String, method: String, body: Body?) async throws -> T {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
}
