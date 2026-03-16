import Foundation

enum CapabilityPresentation {
    static func title(for capabilityID: String) -> String {
        switch capabilityID {
        case "browser_automation":
            return "Browser automation"
        case "local_filesystem":
            return "Workspace files"
        case "screen_capture":
            return "Screen capture"
        case "clipboard":
            return "Clipboard"
        case "apple_shortcuts":
            return "Apple Shortcuts"
        case "apple_automation":
            return "Apple automation"
        default:
            return capabilityID.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func summary(
        description: String,
        tools: [String]
    ) -> String {
        if !description.isEmpty {
            return description
        }
        return tools.joined(separator: ", ")
    }
}
