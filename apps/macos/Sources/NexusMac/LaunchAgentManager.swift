import Darwin
import Foundation

struct LaunchAgentStatus: Equatable, Sendable {
    let installed: Bool
    let loaded: Bool
    let plistPath: String
    let executablePath: String?
}

enum LaunchAgentManager {
    private static let label = "ai.nexus.macos.edge"
    private static let launchAgentFlag = "NEXUS_LAUNCH_AGENT"

    static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    static var launchLogURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Nexus/launchd.log")
    }

    static func status() async -> LaunchAgentStatus {
        let installed = FileManager.default.fileExists(atPath: plistURL.path)
        let executablePath = currentExecutablePath()
        guard installed else {
            return LaunchAgentStatus(
                installed: false,
                loaded: false,
                plistPath: plistURL.path,
                executablePath: executablePath
            )
        }
        let result = await Launchctl.run(["print", "gui/\(getuid())/\(label)"])
        return LaunchAgentStatus(
            installed: true,
            loaded: result.status == 0,
            plistPath: plistURL.path,
            executablePath: executablePath
        )
    }

    static func install(executablePath: String? = nil) async -> String? {
        let target = executablePath ?? currentExecutablePath()
        guard let target, !target.isEmpty else {
            return "Unable to determine the current Nexus executable path."
        }
        do {
            try writePlist(executablePath: target)
        } catch {
            return error.localizedDescription
        }

        _ = await Launchctl.run(["bootout", "gui/\(getuid())/\(label)"])
        let bootstrap = await Launchctl.run(["bootstrap", "gui/\(getuid())", plistURL.path])
        if bootstrap.status != 0 {
            return bootstrap.output.trimmingCharacters(in: .whitespacesAndNewlines).nonEmpty ?? "launchctl bootstrap failed"
        }
        let kickstart = await Launchctl.run(["kickstart", "-k", "gui/\(getuid())/\(label)"])
        if kickstart.status != 0 {
            return kickstart.output.trimmingCharacters(in: .whitespacesAndNewlines).nonEmpty ?? "launchctl kickstart failed"
        }
        return nil
    }

    static func uninstall() async -> String? {
        do {
            if FileManager.default.fileExists(atPath: plistURL.path) {
                try FileManager.default.removeItem(at: plistURL)
            }
        } catch {
            return error.localizedDescription
        }

        if ProcessInfo.processInfo.environment[launchAgentFlag] != "1" {
            _ = await Launchctl.run(["bootout", "gui/\(getuid())/\(label)"])
        }
        return nil
    }

    static func unloadForUserQuit() async -> String? {
        let managedByLaunchAgent = ProcessInfo.processInfo.environment[launchAgentFlag] == "1"
        let currentStatus = managedByLaunchAgent ? nil : await status()
        guard managedByLaunchAgent || currentStatus?.loaded == true else {
            return nil
        }

        let result = await Launchctl.run(["bootout", "gui/\(getuid())/\(label)"])
        if result.status == 0 || isBenignBootoutOutput(result.output) {
            return nil
        }
        return result.output.trimmingCharacters(in: .whitespacesAndNewlines).nonEmpty ?? "launchctl bootout failed"
    }

    static func makePlistContents(executablePath: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let pathValue = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        let executable = xmlEscaped(executablePath)
        let workingDirectory = xmlEscaped(home)
        let stdoutPath = xmlEscaped(launchLogURL.path)
        let stderrPath = xmlEscaped(launchLogURL.path)
        let pathEnvironment = xmlEscaped(pathValue)

        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key>
          <string>\(label)</string>
          <key>ProgramArguments</key>
          <array>
            <string>\(executable)</string>
          </array>
          <key>WorkingDirectory</key>
          <string>\(workingDirectory)</string>
          <key>RunAtLoad</key>
          <true/>
          <key>KeepAlive</key>
          <dict>
            <key>SuccessfulExit</key>
            <false/>
          </dict>
          <key>EnvironmentVariables</key>
          <dict>
            <key>PATH</key>
            <string>\(pathEnvironment)</string>
            <key>\(launchAgentFlag)</key>
            <string>1</string>
          </dict>
          <key>StandardOutPath</key>
          <string>\(stdoutPath)</string>
          <key>StandardErrorPath</key>
          <string>\(stderrPath)</string>
        </dict>
        </plist>
        """
    }

    private static func writePlist(executablePath: String) throws {
        let fm = FileManager.default
        try fm.createDirectory(
            at: plistURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: nil
        )
        try fm.createDirectory(
            at: launchLogURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: nil
        )
        let plist = makePlistContents(executablePath: executablePath)
        try plist.write(to: plistURL, atomically: true, encoding: .utf8)
    }

    private static func currentExecutablePath() -> String? {
        if let path = Bundle.main.executableURL?.path, !path.isEmpty {
            return path
        }
        let fallback = CommandLine.arguments.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return fallback.isEmpty ? nil : fallback
    }

    private static func xmlEscaped(_ value: String) -> String {
        value
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }

    private static func isBenignBootoutOutput(_ output: String) -> Bool {
        let normalized = output.lowercased()
        return normalized.contains("no such process")
            || normalized.contains("service not found")
            || normalized.contains("could not find service")
            || normalized.contains("not loaded")
    }
}

private extension String {
    var nonEmpty: String? {
        isEmpty ? nil : self
    }
}
