import Foundation

extension Process {
    func runAndReadToEnd(from pipe: Pipe) throws -> Data {
        try self.run()
        let data = pipe.fileHandleForReading.readToEndSafely()
        self.waitUntilExit()
        return data
    }
}
