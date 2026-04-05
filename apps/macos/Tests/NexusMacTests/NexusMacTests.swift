import XCTest
@testable import NexusMac

final class NexusMacTests: XCTestCase {
    func testLaunchCommandUsesDirectEnvPythonWhenAvailable() throws {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let binDir = tempRoot.appendingPathComponent("bin", isDirectory: true)
        let envBinDir = tempRoot
            .appendingPathComponent("envs", isDirectory: true)
            .appendingPathComponent("ai_assist", isDirectory: true)
            .appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: binDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: envBinDir, withIntermediateDirectories: true)

        let condaPath = binDir.appendingPathComponent("conda")
        let pythonPath = envBinDir.appendingPathComponent("python")
        FileManager.default.createFile(atPath: condaPath.path, contents: Data(), attributes: [.posixPermissions: 0o755])
        FileManager.default.createFile(atPath: pythonPath.path, contents: Data(), attributes: [.posixPermissions: 0o755])

        let settings = SidecarSettings(
            nexusRoot: "/tmp/Nexus",
            nodeCardPath: "/tmp/Nexus/config/node_cards/mac.yaml",
            condaExecutable: condaPath.path,
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: "10.0.0.9",
            brokerPort: 1883,
            hubAPIHost: "10.0.0.10",
            hubAPIPort: 18100,
            meshTransport: "tcp",
            autoStartSidecar: true
        )

        let command = settings.makeLaunchCommand()
        XCTAssertEqual(command.executable, pythonPath.path)
        XCTAssertEqual(command.arguments.prefix(2), ["-m", "nexus.edge.macos_sidecar"])
        XCTAssertFalse(command.arguments.contains("ai_assist"))
    }

    func testLaunchCommandUsesAiAssistEnvironment() {
        let settings = SidecarSettings(
            nexusRoot: "/tmp/Nexus",
            nodeCardPath: "/tmp/Nexus/config/node_cards/mac.yaml",
            condaExecutable: "/opt/miniconda3/bin/conda",
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: "10.0.0.9",
            brokerPort: 1883,
            hubAPIHost: "10.0.0.10",
            hubAPIPort: 18100,
            meshTransport: "tcp",
            autoStartSidecar: true
        )

        let command = settings.makeLaunchCommand()
        XCTAssertEqual(command.executable, "/opt/miniconda3/bin/conda")
        XCTAssertTrue(command.arguments.contains("ai_assist"))
        XCTAssertTrue(command.arguments.contains("nexus.edge.macos_sidecar"))
        XCTAssertTrue(command.arguments.contains("10.0.0.9"))
        XCTAssertEqual(command.currentDirectory, "/tmp/Nexus")
    }

    func testEnvFallbackCommandWrapsCondaWithUsrBinEnv() {
        var settings = SidecarSettings.defaultSettings()
        settings.condaExecutable = "conda"

        let command = settings.makeLaunchCommand()
        XCTAssertEqual(command.executable, "/usr/bin/env")
        XCTAssertEqual(command.arguments.first, "conda")
    }

    func testMLXLaunchCommandUsesExecutableServeScriptWhenAvailable() throws {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)

        let servePath = tempRoot.appendingPathComponent("serve.sh")
        FileManager.default.createFile(atPath: servePath.path, contents: Data(), attributes: [.posixPermissions: 0o755])

        let settings = MLXLocalAPISettings(
            root: tempRoot.path,
            serveScriptPath: servePath.path,
            host: "127.0.0.1",
            port: 8008
        )

        let command = settings.makeLaunchCommand()
        XCTAssertEqual(command.executable, servePath.path)
        XCTAssertEqual(command.arguments, [])
        XCTAssertEqual(command.currentDirectory, tempRoot.path)
    }

    func testMLXLaunchCommandFallsBackToZshForNonExecutableScript() throws {
        let tempRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot, withIntermediateDirectories: true)

        let servePath = tempRoot.appendingPathComponent("serve.sh")
        FileManager.default.createFile(atPath: servePath.path, contents: Data("echo test".utf8))

        let settings = MLXLocalAPISettings(
            root: tempRoot.path,
            serveScriptPath: servePath.path,
            host: "127.0.0.1",
            port: 8008
        )

        let command = settings.makeLaunchCommand()
        XCTAssertEqual(command.executable, "/bin/zsh")
        XCTAssertEqual(command.arguments, [servePath.path])
    }

    func testMLXHealthResponseDecodesLegacyLocalServerShape() throws {
        let data = Data(
            """
            {
              "status": "ok",
              "model": "mlx-community/Qwen3.5-9B-OptiQ-4bit",
              "loaded": true,
              "device": "gpu",
              "cache_dir": "/tmp/hf"
            }
            """.utf8
        )

        let health = try JSONDecoder().decode(MLXLocalAPIHealthResponse.self, from: data)
        XCTAssertEqual(health.status, "ok")
        XCTAssertEqual(health.model, "mlx-community/Qwen3.5-9B-OptiQ-4bit")
        XCTAssertTrue(health.loaded)
        XCTAssertEqual(health.device, "gpu")
        XCTAssertEqual(health.cacheDir, "/tmp/hf")
    }

    func testMLXHealthResponseDecodesMLXVLMShape() throws {
        let data = Data(
            """
            {
              "status": "healthy",
              "loaded_model": "mlx-community/gemma-4-e4b-it-4bit",
              "loaded_adapter": null
            }
            """.utf8
        )

        let health = try JSONDecoder().decode(MLXLocalAPIHealthResponse.self, from: data)
        XCTAssertEqual(health.status, "healthy")
        XCTAssertEqual(health.model, "mlx-community/gemma-4-e4b-it-4bit")
        XCTAssertTrue(health.loaded)
        XCTAssertNil(health.device)
        XCTAssertNil(health.cacheDir)
    }

    @MainActor
    func testCollectProcessOutputHandlesLargeStdoutWithoutBlocking() {
        let output = SidecarSupervisor.collectProcessOutput(
            executable: "/bin/sh",
            arguments: ["-c", "python3 - <<'PY'\nprint('x' * 200000)\nPY"]
        )

        XCTAssertGreaterThan(output.count, 150000)
        XCTAssertTrue(output.contains("xxxxx"))
    }

    func testLaunchAgentPlistUsesCurrentExecutable() {
        let plist = LaunchAgentManager.makePlistContents(executablePath: "/tmp/NexusMac")
        XCTAssertTrue(plist.contains("ai.nexus.macos.edge"))
        XCTAssertTrue(plist.contains("/tmp/NexusMac"))
        XCTAssertTrue(plist.contains("NEXUS_LAUNCH_AGENT"))
        XCTAssertTrue(plist.contains("<key>SuccessfulExit</key>"))
        XCTAssertFalse(plist.contains("<key>KeepAlive</key>\n          <true/>"))
    }

    func testSettingsMergeFillsMissingHubDefaults() {
        let fallback = SidecarSettings.defaultSettings()
        let partial = SidecarSettings(
            nexusRoot: "/tmp/Nexus",
            nodeCardPath: "",
            condaExecutable: "",
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: "",
            brokerPort: 0,
            hubAPIHost: "",
            hubAPIPort: 0,
            meshTransport: "",
            autoStartSidecar: true
        )

        let merged = partial.merged(with: fallback)
        XCTAssertEqual(merged.brokerHost, "100.121.67.94")
        XCTAssertEqual(merged.brokerPort, 1883)
        XCTAssertEqual(merged.hubAPIHost, "100.121.67.94")
        XCTAssertEqual(merged.hubAPIPort, 18100)
        XCTAssertEqual(merged.meshTransport, "tcp")
    }

    func testResolvedHubAPIHostFallsBackToBrokerHost() {
        let settings = SidecarSettings(
            nexusRoot: "/tmp/Nexus",
            nodeCardPath: "/tmp/Nexus/config/node_cards/mac.yaml",
            condaExecutable: "/opt/miniconda3/bin/conda",
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: "10.0.0.9",
            brokerPort: 1883,
            hubAPIHost: "",
            hubAPIPort: 18100,
            meshTransport: "tcp",
            autoStartSidecar: true
        )

        XCTAssertEqual(settings.resolvedHubAPIHost, "10.0.0.9")
    }

    func testResolvedHubBearerTokenFallsBackToStoredValue() {
        let settings = SidecarSettings(
            nexusRoot: "/tmp/Nexus",
            nodeCardPath: "/tmp/Nexus/config/node_cards/mac.yaml",
            condaExecutable: "/opt/miniconda3/bin/conda",
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: "10.0.0.9",
            brokerPort: 1883,
            hubAPIHost: "10.0.0.10",
            hubAPIPort: 18100,
            hubBearerToken: "token-123",
            meshTransport: "tcp",
            autoStartSidecar: true
        )

        XCTAssertEqual(settings.resolvedHubBearerToken, "token-123")
    }

    func testLaunchCommandDoesNotExposeHubTokenInArguments() {
        let settings = SidecarSettings(
            nexusRoot: "/tmp/Nexus",
            nodeCardPath: "/tmp/Nexus/config/node_cards/mac.yaml",
            condaExecutable: "/opt/miniconda3/bin/conda",
            localHost: "127.0.0.1",
            localPort: 8765,
            brokerHost: "10.0.0.9",
            brokerPort: 1883,
            hubAPIHost: "10.0.0.10",
            hubAPIPort: 18100,
            hubBearerToken: "top-secret",
            meshTransport: "tcp",
            autoStartSidecar: true
        )

        let command = settings.makeLaunchCommand()
        XCTAssertFalse(command.arguments.contains("top-secret"))
        XCTAssertFalse(command.arguments.contains("--hub-api-bearer-token"))
    }

    func testCapabilityPresentationTitleMapsKnownCapability() {
        XCTAssertEqual(CapabilityPresentation.title(for: "browser_automation"), "Browser automation")
    }

    func testCapabilityPresentationSummaryFallsBackToTools() {
        XCTAssertEqual(
            CapabilityPresentation.summary(description: "", tools: ["read_clipboard", "write_clipboard"]),
            "read_clipboard, write_clipboard"
        )
    }

    func testPermissionDiagnosticsMapsAutomationPromptState() {
        let check = PermissionDiagnostics.mapAutomationState(
            status: OSStatus(errAEEventWouldRequireUserConsent),
            appName: "Google Chrome"
        )
        XCTAssertEqual(check.state, .needsPrompt)
        XCTAssertTrue(check.detail.contains("Automation prompt"))
    }

    func testPermissionDiagnosticsActionTitleForDeniedState() {
        let check = PermissionCheck(
            id: "automation.chrome",
            title: "Automation · Google Chrome",
            detail: "Denied",
            state: .denied,
            systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
        )
        XCTAssertEqual(PermissionDiagnostics.actionTitle(for: check), "Open Settings")
    }

    func testWindowLayoutPlanResetsTooSmallSettingsWindow() {
        let visible = NSRect(x: 0, y: 0, width: 1440, height: 900)
        let frame = NSRect(x: 0, y: 0, width: 720, height: 560)

        XCTAssertTrue(
            WindowLayoutPlan.needsReset(
                frame: frame,
                visibleFrame: visible,
                minimumContentSize: NexusWindowKind.settings.minimumContentSize
            )
        )
    }

    func testWindowLayoutPlanCentersSettingsWindowWithinVisibleFrame() {
        let visible = NSRect(x: 0, y: 0, width: 1440, height: 900)
        let centered = WindowLayoutPlan.centeredFrame(
            visibleFrame: visible,
            preferredContentSize: NexusWindowKind.settings.preferredContentSize,
            minimumContentSize: NexusWindowKind.settings.minimumContentSize
        )

        XCTAssertEqual(centered.width, 960)
        XCTAssertEqual(centered.height, 860)
        XCTAssertEqual(centered.origin.x, 240)
        XCTAssertEqual(centered.origin.y, 20)
    }

    func testMeshTraceWindowHasDedicatedTitleAndAutosaveName() {
        XCTAssertEqual(NexusWindowKind.meshTrace.title, "Nexus Mesh Trace")
        XCTAssertEqual(NexusWindowKind.meshTrace.autosaveName, "NexusMeshTraceWindow.v1")
    }

    func testMeshTraceWindowUsesWideInvestigationLayout() {
        XCTAssertEqual(NexusWindowKind.meshTrace.preferredContentSize, NSSize(width: 1180, height: 820))
        XCTAssertEqual(NexusWindowKind.meshTrace.minimumContentSize, NSSize(width: 980, height: 720))
    }

    @MainActor
    func testSupervisorCanStartTwiceWithFreshPipes() async throws {
        let supervisor = SidecarSupervisor()
        var lines: [String] = []
        supervisor.onOutput = { line in
            lines.append(line)
        }
        let command = SidecarLaunchCommand(
            executable: "/bin/sh",
            arguments: ["-c", "printf 'hello\\n'"],
            currentDirectory: "/tmp"
        )

        try supervisor.start(command: command, environment: ProcessInfo.processInfo.environment)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(supervisor.state, .stopped)

        try supervisor.start(command: command, environment: ProcessInfo.processInfo.environment)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(supervisor.state, .stopped)
        XCTAssertEqual(lines.filter { $0 == "hello" }.count, 2)
    }

    @MainActor
    func testMLXSupervisorCanStartTwiceWithFreshPipes() async throws {
        let supervisor = MLXLocalAPISupervisor()
        var lines: [String] = []
        supervisor.onOutput = { line in
            lines.append(line)
        }
        let command = SidecarLaunchCommand(
            executable: "/bin/sh",
            arguments: ["-c", "printf 'mlx\\n'"],
            currentDirectory: "/tmp"
        )

        try supervisor.start(command: command, environment: ProcessInfo.processInfo.environment)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(supervisor.state, .stopped)

        try supervisor.start(command: command, environment: ProcessInfo.processInfo.environment)
        try await Task.sleep(for: .milliseconds(150))
        XCTAssertEqual(supervisor.state, .stopped)
        XCTAssertEqual(lines.filter { $0 == "mlx" }.count, 2)
    }
}
