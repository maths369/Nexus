import Foundation

enum Launchctl {
    struct Result: Sendable {
        let status: Int32
        let output: String
    }

    @discardableResult
    static func run(_ args: [String]) async -> Result {
        await Task.detached(priority: .utility) { () -> Result in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            process.arguments = args
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            do {
                let data = try process.runAndReadToEnd(from: pipe)
                let output = String(data: data, encoding: .utf8) ?? ""
                return Result(status: process.terminationStatus, output: output)
            } catch {
                return Result(status: -1, output: error.localizedDescription)
            }
        }.value
    }
}
