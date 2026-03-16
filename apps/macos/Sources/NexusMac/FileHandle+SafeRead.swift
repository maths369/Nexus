import Foundation

extension FileHandle {
    func readToEndSafely() -> Data {
        do {
            return try self.readToEnd() ?? Data()
        } catch {
            return Data()
        }
    }

    func readSafely(upToCount count: Int) -> Data {
        do {
            return try self.read(upToCount: count) ?? Data()
        } catch {
            return Data()
        }
    }
}
