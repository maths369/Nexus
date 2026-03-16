import Foundation

struct SidecarSettings: Codable, Equatable {
    var nexusRoot: String
    var nodeCardPath: String
    var condaExecutable: String
    var localHost: String
    var localPort: Int
    var brokerHost: String
    var brokerPort: Int
    var hubAPIHost: String
    var hubAPIPort: Int
    var meshTransport: String
    var autoStartSidecar: Bool
    var commandMode: CommandMode

    static let userDefaultsKey = "ai.nexus.macos.sidecar-settings.v1"

    private enum CodingKeys: String, CodingKey {
        case nexusRoot
        case nodeCardPath
        case condaExecutable
        case localHost
        case localPort
        case brokerHost
        case brokerPort
        case hubAPIHost
        case hubAPIPort
        case meshTransport
        case autoStartSidecar
        case commandMode
    }

    init(
        nexusRoot: String,
        nodeCardPath: String,
        condaExecutable: String,
        localHost: String,
        localPort: Int,
        brokerHost: String,
        brokerPort: Int,
        hubAPIHost: String,
        hubAPIPort: Int,
        meshTransport: String,
        autoStartSidecar: Bool,
        commandMode: CommandMode = .auto
    ) {
        self.nexusRoot = nexusRoot
        self.nodeCardPath = nodeCardPath
        self.condaExecutable = condaExecutable
        self.localHost = localHost
        self.localPort = localPort
        self.brokerHost = brokerHost
        self.brokerPort = brokerPort
        self.hubAPIHost = hubAPIHost
        self.hubAPIPort = hubAPIPort
        self.meshTransport = meshTransport
        self.autoStartSidecar = autoStartSidecar
        self.commandMode = commandMode
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        nexusRoot = try container.decode(String.self, forKey: .nexusRoot)
        nodeCardPath = try container.decode(String.self, forKey: .nodeCardPath)
        condaExecutable = try container.decode(String.self, forKey: .condaExecutable)
        localHost = try container.decode(String.self, forKey: .localHost)
        localPort = try container.decode(Int.self, forKey: .localPort)
        brokerHost = try container.decode(String.self, forKey: .brokerHost)
        brokerPort = try container.decode(Int.self, forKey: .brokerPort)
        hubAPIHost = try container.decodeIfPresent(String.self, forKey: .hubAPIHost) ?? ""
        hubAPIPort = try container.decodeIfPresent(Int.self, forKey: .hubAPIPort) ?? 0
        meshTransport = try container.decode(String.self, forKey: .meshTransport)
        autoStartSidecar = try container.decode(Bool.self, forKey: .autoStartSidecar)
        commandMode = try container.decodeIfPresent(CommandMode.self, forKey: .commandMode) ?? .auto
    }

    static func load() -> SidecarSettings {
        let defaults = UserDefaults.standard
        let fallback = defaultSettings()
        guard
            let raw = defaults.data(forKey: userDefaultsKey),
            let decoded = try? JSONDecoder().decode(SidecarSettings.self, from: raw)
        else {
            return fallback
        }
        return decoded.merged(with: fallback)
    }

    func save() {
        let defaults = UserDefaults.standard
        if let encoded = try? JSONEncoder().encode(self) {
            defaults.set(encoded, forKey: Self.userDefaultsKey)
        }
    }

    static func defaultSettings() -> SidecarSettings {
        let root = defaultNexusRoot()
        return SidecarSettings(
            nexusRoot: root,
            nodeCardPath: "\(root)/config/node_cards/macbook-pro.example.yaml",
            condaExecutable: defaultCondaExecutable(),
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: defaultBrokerHost(),
            brokerPort: 1883,
            hubAPIHost: defaultHubAPIHost(),
            hubAPIPort: defaultHubAPIPort(),
            meshTransport: "tcp",
            autoStartSidecar: true
        )
    }

    static func defaultNexusRoot() -> String {
        let env = ProcessInfo.processInfo.environment["NEXUS_ROOT"] ?? ""
        if !env.isEmpty {
            return env
        }
        return "~/Workspace/Nexus"
    }

    static func defaultCondaExecutable() -> String {
        if let conda = ProcessInfo.processInfo.environment["CONDA_EXE"], !conda.isEmpty {
            return conda
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(home)/miniconda3/bin/conda",
            "\(home)/anaconda3/bin/conda",
            "/opt/homebrew/Caskroom/miniconda/base/bin/conda",
            "/usr/local/Caskroom/miniconda/base/bin/conda",
        ]
        for candidate in candidates where FileManager.default.fileExists(atPath: candidate) {
            return candidate
        }
        return "conda"
    }

    static func defaultBrokerHost() -> String {
        let env = ProcessInfo.processInfo.environment["NEXUS_MAC_BROKER_HOST"] ?? ""
        if !env.isEmpty {
            return env
        }
        return "YOUR_HUB_IP"
    }

    static func defaultHubAPIHost() -> String {
        let env = ProcessInfo.processInfo.environment["NEXUS_MAC_HUB_API_HOST"] ?? ""
        if !env.isEmpty {
            return env
        }
        return defaultBrokerHost()
    }

    static func defaultHubAPIPort() -> Int {
        let env = ProcessInfo.processInfo.environment["NEXUS_MAC_HUB_API_PORT"] ?? ""
        if let port = Int(env), port > 0 {
            return port
        }
        return 18100
    }

    func merged(with fallback: SidecarSettings) -> SidecarSettings {
        SidecarSettings(
            nexusRoot: nexusRoot.nonEmpty ?? fallback.nexusRoot,
            nodeCardPath: nodeCardPath.nonEmpty ?? fallback.nodeCardPath,
            condaExecutable: condaExecutable.nonEmpty ?? fallback.condaExecutable,
            localHost: localHost.nonEmpty ?? fallback.localHost,
            localPort: localPort > 0 ? localPort : fallback.localPort,
            brokerHost: brokerHost.nonEmpty ?? fallback.brokerHost,
            brokerPort: brokerPort > 0 ? brokerPort : fallback.brokerPort,
            hubAPIHost: hubAPIHost.nonEmpty ?? brokerHost.nonEmpty ?? fallback.hubAPIHost,
            hubAPIPort: hubAPIPort > 0 ? hubAPIPort : fallback.hubAPIPort,
            meshTransport: meshTransport.nonEmpty ?? fallback.meshTransport,
            autoStartSidecar: autoStartSidecar,
            commandMode: commandMode
        )
    }

    var resolvedHubAPIHost: String {
        hubAPIHost.nonEmpty ?? brokerHost.nonEmpty ?? Self.defaultHubAPIHost()
    }

    var resolvedHubAPIPort: Int {
        hubAPIPort > 0 ? hubAPIPort : Self.defaultHubAPIPort()
    }

    func makeLaunchCommand() -> SidecarLaunchCommand {
        var sidecarArgs = [
            "-m",
            "nexus.edge.macos_sidecar",
            "--root", nexusRoot,
            "--node-card-path", nodeCardPath,
            "--http-host", localHost,
            "--http-port", String(localPort),
        ]

        let trimmedBroker = brokerHost.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedBroker.isEmpty {
            sidecarArgs += ["--broker-host", trimmedBroker]
            sidecarArgs += ["--broker-port", String(brokerPort)]
        }

        let trimmedTransport = meshTransport.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedTransport.isEmpty {
            sidecarArgs += ["--mesh-transport", trimmedTransport]
        }

        if let directPython = resolvedEnvPythonExecutable() {
            return SidecarLaunchCommand(
                executable: directPython,
                arguments: sidecarArgs,
                currentDirectory: nexusRoot
            )
        }

        let condaArgs = [
            "run",
            "--no-capture-output",
            "-n",
            "ai_assist",
            "python",
        ] + sidecarArgs

        if condaExecutable.contains("/") {
            return SidecarLaunchCommand(
                executable: condaExecutable,
                arguments: condaArgs,
                currentDirectory: nexusRoot
            )
        }

        return SidecarLaunchCommand(
            executable: "/usr/bin/env",
            arguments: [condaExecutable] + condaArgs,
            currentDirectory: nexusRoot
        )
    }

    func resolvedEnvPythonExecutable() -> String? {
        guard condaExecutable.contains("/") else {
            return nil
        }
        let condaURL = URL(fileURLWithPath: condaExecutable)
        let base = condaURL.deletingLastPathComponent().deletingLastPathComponent()
        let pythonURL = base
            .appendingPathComponent("envs")
            .appendingPathComponent("ai_assist")
            .appendingPathComponent("bin")
            .appendingPathComponent("python")
        let path = pythonURL.path
        return FileManager.default.isExecutableFile(atPath: path) ? path : nil
    }
}

private extension String {
    var nonEmpty: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

@MainActor
final class SidecarSupervisor {
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
    private var activeSettings: SidecarSettings?

    var onOutput: ((String) -> Void)?
    var onStateChange: ((State) -> Void)?
    var onFailure: ((String) -> Void)?

    var isRunning: Bool {
        process?.isRunning == true
    }

    func start(settings: SidecarSettings) throws {
        terminateMatchingSidecars(settings: settings)
        let command = settings.makeLaunchCommand()
        try start(
            command: command,
            environment: ProcessInfo.processInfo.environment.merging([
            "NEXUS_ROOT": settings.nexusRoot,
            "PYTHONUNBUFFERED": "1",
            ]) { _, new in new }
        )
        activeSettings = settings
    }

    func stop() {
        cleanupActivePipes()
        if let pid = process?.processIdentifier {
            terminateProcessTree(rootPID: pid)
        }
        if let settings = activeSettings {
            terminateMatchingSidecars(settings: settings)
        }
        process = nil
        activeSettings = nil
        updateState(.stopped)
    }

    func restart(settings: SidecarSettings) throws {
        stop()
        try start(settings: settings)
    }

    private func updateState(_ state: State) {
        self.state = state
        onStateChange?(state)
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
                        self?.onFailure?("Local engine exited with status \(terminated.terminationStatus)")
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

    private func terminateMatchingSidecars(settings: SidecarSettings) {
        let candidates = findMatchingSidecarPIDs(settings: settings)
        guard !candidates.isEmpty else { return }
        terminateProcesses(candidates)
    }

    private func findMatchingSidecarPIDs(settings: SidecarSettings) -> [pid_t] {
        let output = runHelper(executable: "/bin/ps", arguments: ["-Ao", "pid=,command="])
        guard !output.isEmpty else { return [] }
        let expectedRoot = "--root \(settings.nexusRoot)"
        let expectedPort = "--http-port \(settings.localPort)"

        return output
            .split(whereSeparator: \.isNewline)
            .compactMap { rawLine -> pid_t? in
                let line = String(rawLine)
                guard line.contains("nexus.edge.macos_sidecar") else { return nil }
                guard line.contains(expectedRoot), line.contains(expectedPort) else { return nil }
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
        let output = runHelper(executable: "/bin/ps", arguments: ["-Ao", "pid=,ppid="])
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

    private func runHelper(executable: String, arguments: [String]) -> String {
        Self.collectProcessOutput(executable: executable, arguments: arguments)
    }

    static func collectProcessOutput(executable: String, arguments: [String]) -> String {
        let helper = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        helper.executableURL = URL(fileURLWithPath: executable)
        helper.arguments = arguments
        helper.standardOutput = stdoutPipe
        helper.standardError = stderrPipe
        do {
            try helper.run()
        } catch {
            return ""
        }

        let stdoutHandle = stdoutPipe.fileHandleForReading
        let stderrHandle = stderrPipe.fileHandleForReading
        let stdoutData = stdoutHandle.readDataToEndOfFile()
        _ = stderrHandle.readDataToEndOfFile()
        helper.waitUntilExit()
        return String(data: stdoutData, encoding: .utf8) ?? ""
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
