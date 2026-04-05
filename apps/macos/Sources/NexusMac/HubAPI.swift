import Foundation

struct HubHealthResponse: Decodable {
    let status: String
    let version: String
}

struct HubMessageEnvelope: Decodable {
    let type: String
    let sessionID: String?
    let content: String

    enum CodingKeys: String, CodingKey {
        case type
        case sessionID = "session_id"
        case content
    }
}

struct HubConversationEntry: Identifiable, Equatable {
    enum Role: Equatable {
        case user
        case assistant
        case system
    }

    enum Kind: String, Equatable {
        case command
        case ack
        case status
        case blocked
        case result
        case clarify
        case error
        case note
    }

    let id: UUID
    let role: Role
    let kind: Kind
    let content: String
    let sessionID: String?
    let createdAt: Date

    init(
        id: UUID = UUID(),
        role: Role,
        kind: Kind,
        content: String,
        sessionID: String? = nil,
        createdAt: Date = .now
    ) {
        self.id = id
        self.role = role
        self.kind = kind
        self.content = content
        self.sessionID = sessionID
        self.createdAt = createdAt
    }

    static func fromHubEnvelope(_ envelope: HubMessageEnvelope) -> HubConversationEntry {
        let kind = Kind(rawValue: envelope.type.lowercased()) ?? .note
        let role: Role = kind == .error ? .system : .assistant
        return HubConversationEntry(
            role: role,
            kind: kind,
            content: envelope.content,
            sessionID: envelope.sessionID
        )
    }
}

enum HubAPIClientError: LocalizedError {
    case timeout
    case unexpectedMessage

    var errorDescription: String? {
        switch self {
        case .timeout:
            return "Timed out waiting for Nexus Hub."
        case .unexpectedMessage:
            return "Received an unexpected response from Nexus Hub."
        }
    }
}

private struct HubSendPayload: Encodable {
    let type: String
    let seq: Int
    let senderID: String
    let content: String

    enum CodingKeys: String, CodingKey {
        case type
        case seq
        case senderID = "sender_id"
        case content
    }
}

final class HubAPIClient {
    private let httpBaseURL: URL
    private let websocketURL: URL
    private let session: URLSession
    private let bearerToken: String

    init(host: String, port: Int, bearerToken: String = "", session: URLSession = .shared) {
        self.httpBaseURL = URL(string: "http://\(host):\(port)")!
        self.websocketURL = URL(string: "ws://\(host):\(port)/ws")!
        self.session = session
        self.bearerToken = bearerToken
    }

    func health() async throws -> HubHealthResponse {
        var request = URLRequest(url: httpBaseURL.appending(path: "health"))
        request.httpMethod = "GET"
        applyAuth(to: &request)
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(HubHealthResponse.self, from: data)
    }

    func sendMessage(
        content: String,
        senderID: String,
        sequence: Int = Int(Date().timeIntervalSince1970),
        onEvent: @escaping @MainActor (HubConversationEntry) -> Void
    ) async throws {
        var request = URLRequest(url: websocketURL)
        applyAuth(to: &request)
        let task = session.webSocketTask(with: request)
        task.resume()
        defer {
            task.cancel(with: .goingAway, reason: nil)
        }

        let payload = HubSendPayload(type: "message", seq: sequence, senderID: senderID, content: content)
        let encoded = try JSONEncoder().encode(payload)
        guard let message = String(data: encoded, encoding: .utf8) else {
            throw HubAPIClientError.unexpectedMessage
        }
        try await task.send(.string(message))

        while true {
            let envelope = try await receiveEnvelope(from: task)
            await onEvent(HubConversationEntry.fromHubEnvelope(envelope))

            switch envelope.type.lowercased() {
            case "result", "error", "blocked", "clarify":
                return
            default:
                continue
            }
        }
    }

    private func receiveEnvelope(from task: URLSessionWebSocketTask) async throws -> HubMessageEnvelope {
        let message = try await withThrowingTaskGroup(of: URLSessionWebSocketTask.Message.self) { group in
            group.addTask {
                try await task.receive()
            }
            group.addTask {
                try await Task.sleep(for: .seconds(60))
                throw HubAPIClientError.timeout
            }
            let result = try await group.next()
            group.cancelAll()
            guard let result else {
                throw HubAPIClientError.unexpectedMessage
            }
            return result
        }

        let data: Data
        switch message {
        case let .string(text):
            data = Data(text.utf8)
        case let .data(binary):
            data = binary
        @unknown default:
            throw HubAPIClientError.unexpectedMessage
        }

        return try JSONDecoder().decode(HubMessageEnvelope.self, from: data)
    }

    private func applyAuth(to request: inout URLRequest) {
        let token = bearerToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }
}
