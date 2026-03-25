import Foundation

struct MLXLocalAPISettings: Equatable {
    var root: String
    var serveScriptPath: String
    var serverScriptPath: String
    var host: String
    var port: Int

    init(
        root: String,
        serveScriptPath: String,
        serverScriptPath: String? = nil,
        host: String,
        port: Int
    ) {
        self.root = root
        self.serveScriptPath = serveScriptPath
        self.serverScriptPath = serverScriptPath ?? (root as NSString).appendingPathComponent("server.py")
        self.host = host
        self.port = port
    }

    static func defaultSettings() -> MLXLocalAPISettings {
        let environment = ProcessInfo.processInfo.environment
        let root = resolvedValue(
            environment["NEXUS_MLX_LOCAL_API_ROOT"],
            fallback: defaultRoot()
        )
        let config = loadConfig(atRoot: root)
        let host = resolvedValue(
            environment["NEXUS_MLX_LOCAL_API_HOST"],
            fallback: stringValue(config["host"]) ?? "127.0.0.1"
        )
        let port = Int(environment["NEXUS_MLX_LOCAL_API_PORT"] ?? "")
            ?? intValue(config["port"])
            ?? 8008
        let serveScriptPath = resolvedValue(
            environment["NEXUS_MLX_LOCAL_API_SERVE_SCRIPT"],
            fallback: (root as NSString).appendingPathComponent("serve.sh")
        )
        return MLXLocalAPISettings(
            root: root,
            serveScriptPath: serveScriptPath,
            host: host,
            port: port
        )
    }

    static func defaultRoot() -> String {
        if
            let resourcePath = Bundle.main.resourcePath,
            FileManager.default.fileExists(atPath: (resourcePath as NSString).appendingPathComponent("mlx-local-api"))
        {
            return (resourcePath as NSString).appendingPathComponent("mlx-local-api")
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return "\(home)/Workspace/mlx-local-api"
    }

    func makeLaunchCommand() -> SidecarLaunchCommand {
        if FileManager.default.isExecutableFile(atPath: serveScriptPath) {
            return SidecarLaunchCommand(
                executable: serveScriptPath,
                arguments: [],
                currentDirectory: root
            )
        }
        return SidecarLaunchCommand(
            executable: "/bin/zsh",
            arguments: [serveScriptPath],
            currentDirectory: root
        )
    }

    private static func loadConfig(atRoot root: String) -> [String: Any] {
        let configPath = (root as NSString).appendingPathComponent("config.json")
        guard let data = FileManager.default.contents(atPath: configPath) else {
            return [:]
        }
        guard let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return [:]
        }
        return raw
    }

    private static func resolvedValue(_ candidate: String?, fallback: String) -> String {
        let trimmed = candidate?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? fallback : trimmed
    }

    private static func stringValue(_ value: Any?) -> String? {
        guard let text = value as? String else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func intValue(_ value: Any?) -> Int? {
        switch value {
        case let number as Int:
            return number
        case let number as NSNumber:
            return number.intValue
        case let text as String:
            return Int(text)
        default:
            return nil
        }
    }
}

struct MLXLocalAPIHealthResponse: Decodable {
    let status: String
    let model: String
    let loaded: Bool
    let device: String
    let cacheDir: String

    enum CodingKeys: String, CodingKey {
        case status
        case model
        case loaded
        case device
        case cacheDir = "cache_dir"
    }
}

final class MLXLocalAPIClient {
    private let baseURL: URL
    private let session: URLSession

    init(host: String, port: Int, session: URLSession = .shared) {
        self.baseURL = URL(string: "http://\(host):\(port)")!
        self.session = session
    }

    func health() async throws -> MLXLocalAPIHealthResponse {
        var request = URLRequest(url: baseURL.appending(path: "health"))
        request.httpMethod = "GET"
        let (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder().decode(MLXLocalAPIHealthResponse.self, from: data)
    }
}

@MainActor
final class MLXLocalAPISupervisor {
    enum State: Equatable {
        case stopped
        case starting
        case running
    }

    private struct ProcessPipes {
        let stdout: Pipe
        let stderr: Pipe
    }

    private(set) var state: State = .stopped
    private var process: Process?
    private var activePipes: ProcessPipes?
    private var activeSettings: MLXLocalAPISettings?

    var onOutput: ((String) -> Void)?
    var onStateChange: ((State) -> Void)?
    var onFailure: ((String) -> Void)?

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start(settings: MLXLocalAPISettings) throws {
        terminateMatchingServices(settings: settings)
        try start(
            command: settings.makeLaunchCommand(),
            environment: ProcessInfo.processInfo.environment
        )
        activeSettings = settings
    }

    func stop(settings: MLXLocalAPISettings? = nil) {
        cleanupActivePipes()
        if let pid = process?.processIdentifier {
            terminateProcessTree(rootPID: pid)
        }
        if let settings = settings ?? activeSettings {
            terminateMatchingServices(settings: settings)
        }
        process = nil
        activeSettings = nil
        updateState(.stopped)
    }

    func restart(settings: MLXLocalAPISettings) throws {
        stop(settings: settings)
        try start(settings: settings)
    }

    func start(command: SidecarLaunchCommand, environment: [String: String] = [:]) throws {
        guard !isRunning else { return }

        let process = Process()
        let pipes = ProcessPipes(stdout: Pipe(), stderr: Pipe())
        process.executableURL = URL(fileURLWithPath: command.executable)
        process.arguments = command.arguments
        process.currentDirectoryURL = URL(fileURLWithPath: command.currentDirectory)
        process.standardOutput = pipes.stdout
        process.standardError = pipes.stderr
        process.environment = environment

        pipes.stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let lines = Self.readAvailableLines(from: handle)
            guard !lines.isEmpty else { return }
            Task { @MainActor in
                self?.emitOutput(lines)
            }
        }
        pipes.stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let lines = Self.readAvailableLines(from: handle)
            guard !lines.isEmpty else { return }
            Task { @MainActor in
                self?.emitOutput(lines)
            }
        }

        process.terminationHandler = { [weak self, pipes] terminated in
            Task { @MainActor in
                self?.cleanup(pipes: pipes)
                if self?.process === terminated {
                    self?.process = nil
                    self?.activePipes = nil
                    self?.updateState(.stopped)
                    if terminated.terminationStatus != 0 {
                        self?.onFailure?("MLX local API exited with status \(terminated.terminationStatus)")
                    }
                }
            }
        }

        updateState(.starting)
        try process.run()
        self.process = process
        self.activePipes = pipes
        updateState(.running)
    }

    private func terminateMatchingServices(settings: MLXLocalAPISettings) {
        let candidates = findMatchingServicePIDs(settings: settings)
        guard !candidates.isEmpty else { return }
        terminateProcesses(candidates)
    }

    private func findMatchingServicePIDs(settings: MLXLocalAPISettings) -> [pid_t] {
        let output = SidecarSupervisor.collectProcessOutput(
            executable: "/bin/ps",
            arguments: ["-Ao", "pid=,command="]
        )
        guard !output.isEmpty else { return [] }
        let scriptCandidates = [settings.serverScriptPath, settings.serveScriptPath]

        return output
            .split(whereSeparator: \.isNewline)
            .compactMap { rawLine -> pid_t? in
                let line = String(rawLine)
                guard scriptCandidates.contains(where: { line.contains($0) }) else { return nil }
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                let components = trimmed.split(maxSplits: 1, whereSeparator: \.isWhitespace)
                guard let pidText = components.first, let pid = Int32(pidText) else { return nil }
                return pid
            }
    }

    private func terminateProcessTree(rootPID: pid_t) {
        let descendants = descendantPIDs(of: rootPID)
        terminateProcesses(descendants + [rootPID])
    }

    private func descendantPIDs(of rootPID: pid_t) -> [pid_t] {
        let output = SidecarSupervisor.collectProcessOutput(
            executable: "/bin/ps",
            arguments: ["-Ao", "pid=,ppid="]
        )
        guard !output.isEmpty else { return [] }

        var childrenByParent: [pid_t: [pid_t]] = [:]
        for rawLine in output.split(whereSeparator: \.isNewline) {
            let parts = rawLine.split(whereSeparator: \.isWhitespace)
            guard parts.count >= 2, let pid = Int32(parts[0]), let ppid = Int32(parts[1]) else { continue }
            childrenByParent[ppid, default: []].append(pid)
        }

        var queue: [pid_t] = [rootPID]
        var descendants: [pid_t] = []
        var seen: Set<pid_t> = [rootPID]
        while let current = queue.first {
            queue.removeFirst()
            for child in childrenByParent[current] ?? [] where !seen.contains(child) {
                seen.insert(child)
                descendants.append(child)
                queue.append(child)
            }
        }
        return descendants
    }

    private func terminateProcesses(_ pids: [pid_t]) {
        let unique = Array(Set(pids)).sorted()
        guard !unique.isEmpty else { return }

        for pid in unique {
            _ = Darwin.kill(pid, SIGTERM)
        }
        usleep(250_000)
        for pid in unique where processExists(pid) {
            _ = Darwin.kill(pid, SIGKILL)
        }
    }

    private func processExists(_ pid: pid_t) -> Bool {
        Darwin.kill(pid, 0) == 0 || errno != ESRCH
    }

    private func updateState(_ state: State) {
        self.state = state
        onStateChange?(state)
    }

    private func emitOutput(_ lines: [String]) {
        for line in lines {
            onOutput?(line)
        }
    }

    private nonisolated static func readAvailableLines(from handle: FileHandle) -> [String] {
        let data = handle.readSafely(upToCount: 4096)
        guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return [] }
        return text.split(whereSeparator: \.isNewline).map(String.init)
    }

    private func cleanupActivePipes() {
        guard let pipes = activePipes else { return }
        cleanup(pipes: pipes)
        activePipes = nil
    }

    private func cleanup(pipes: ProcessPipes) {
        pipes.stdout.fileHandleForReading.readabilityHandler = nil
        pipes.stderr.fileHandleForReading.readabilityHandler = nil
    }
}
