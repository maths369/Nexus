import AppKit
import ApplicationServices
import CoreGraphics
import CoreServices
import Foundation
import SwiftUI

enum PermissionState: String, Equatable {
    case granted
    case needsPrompt
    case denied
    case unavailable

    var label: String {
        switch self {
        case .granted:
            return "Granted"
        case .needsPrompt:
            return "Needs Prompt"
        case .denied:
            return "Denied"
        case .unavailable:
            return "Unavailable"
        }
    }
}

struct PermissionCheck: Identifiable, Equatable {
    let id: String
    let title: String
    let detail: String
    let state: PermissionState
    let systemSettingsPane: String?
}

enum PermissionDiagnostics {
    @MainActor
    static func snapshot() -> [PermissionCheck] {
        [
            accessibilityCheck(),
            screenRecordingCheck(),
            automationCheck(bundleID: "com.google.Chrome", appName: "Google Chrome", id: "automation.chrome"),
            automationCheck(bundleID: "com.apple.systemevents", appName: "System Events", id: "automation.system_events"),
        ]
    }

    @MainActor
    static func requestPermission(for id: String) async {
        switch id {
        case "accessibility":
            let options = ["AXTrustedCheckOptionPrompt" as CFString: kCFBooleanTrue as CFBoolean] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
        case "screen_recording":
            _ = CGRequestScreenCaptureAccess()
        case "automation.chrome":
            await requestAutomationPermission(for: "com.google.Chrome")
        case "automation.system_events":
            await requestAutomationPermission(for: "com.apple.systemevents")
        default:
            break
        }
    }

    @MainActor
    static func openSystemSettings(for check: PermissionCheck) {
        guard let pane = check.systemSettingsPane,
              let url = URL(string: pane) else {
            return
        }
        NSWorkspace.shared.open(url)
    }

    @MainActor
    static func stateTone(_ state: PermissionState) -> Color {
        switch state {
        case .granted:
            return NexusPalette.mint
        case .needsPrompt:
            return NexusPalette.amber
        case .denied:
            return NexusPalette.rose
        case .unavailable:
            return NexusPalette.steel
        }
    }

    static func actionTitle(for check: PermissionCheck) -> String? {
        switch check.state {
        case .granted:
            return nil
        case .needsPrompt:
            return "Request"
        case .denied:
            return "Open Settings"
        case .unavailable:
            return check.id.hasPrefix("automation.") ? "Launch & Check" : "Refresh"
        }
    }

    static func mapAutomationState(status: OSStatus, appName: String) -> PermissionCheck {
        switch status {
        case noErr:
            return PermissionCheck(
                id: "automation.\(appName)",
                title: "Automation · \(appName)",
                detail: "Nexus can send Apple Events to \(appName).",
                state: .granted,
                systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
            )
        case OSStatus(errAEEventWouldRequireUserConsent):
            return PermissionCheck(
                id: "automation.\(appName)",
                title: "Automation · \(appName)",
                detail: "macOS has not asked yet. Nexus can trigger the first-time Automation prompt for \(appName).",
                state: .needsPrompt,
                systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
            )
        case OSStatus(errAEEventNotPermitted):
            return PermissionCheck(
                id: "automation.\(appName)",
                title: "Automation · \(appName)",
                detail: "macOS denied Apple Events from Nexus to \(appName). Re-enable it in Privacy & Security > Automation.",
                state: .denied,
                systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
            )
        default:
            return PermissionCheck(
                id: "automation.\(appName)",
                title: "Automation · \(appName)",
                detail: "Automation status for \(appName) is currently unavailable (OSStatus \(status)).",
                state: .unavailable,
                systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
            )
        }
    }

    private static func accessibilityCheck() -> PermissionCheck {
        let trusted = AXIsProcessTrusted()
        return PermissionCheck(
            id: "accessibility",
            title: "Accessibility",
            detail: trusted
                ? "Nexus can use Accessibility-based UI automation."
                : "Required for UI scripting, keystrokes, and deeper app control.",
            state: trusted ? .granted : .needsPrompt,
            systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        )
    }

    private static func screenRecordingCheck() -> PermissionCheck {
        let granted = CGPreflightScreenCaptureAccess()
        return PermissionCheck(
            id: "screen_recording",
            title: "Screen Recording",
            detail: granted
                ? "Nexus can capture screenshots and record the screen."
                : "Required for screen capture and recording tools.",
            state: granted ? .granted : .needsPrompt,
            systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        )
    }

    private static func automationCheck(bundleID: String, appName: String, id: String) -> PermissionCheck {
        guard isTargetRunning(bundleID) else {
            return PermissionCheck(
                id: id,
                title: "Automation · \(appName)",
                detail: "Open \(appName) once so Nexus can check or request Automation permission.",
                state: .unavailable,
                systemSettingsPane: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
            )
        }

        let status = automationPermissionStatus(for: bundleID, askUserIfNeeded: false)
        let mapped = mapAutomationState(status: status, appName: appName)
        return PermissionCheck(
            id: id,
            title: mapped.title,
            detail: mapped.detail,
            state: mapped.state,
            systemSettingsPane: mapped.systemSettingsPane
        )
    }

    private static func isTargetRunning(_ bundleID: String) -> Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty
    }

    private static func automationPermissionStatus(for bundleID: String, askUserIfNeeded: Bool) -> OSStatus {
        let descriptor = NSAppleEventDescriptor(bundleIdentifier: bundleID)
        guard let pointer = descriptor.aeDesc else {
            return OSStatus(procNotFound)
        }
        return AEDeterminePermissionToAutomateTarget(
            pointer,
            AEEventClass(typeWildCard),
            AEEventID(typeWildCard),
            askUserIfNeeded
        )
    }

    private static func requestAutomationPermission(for bundleID: String) async {
        if !isTargetRunning(bundleID) {
            launchTarget(bundleID: bundleID)
            try? await Task.sleep(for: .seconds(1))
        }

        _ = await Task.detached(priority: .userInitiated) {
            automationPermissionStatus(for: bundleID, askUserIfNeeded: true)
        }.value
    }

    private static func launchTarget(bundleID: String) {
        guard let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
            return
        }
        let configuration = NSWorkspace.OpenConfiguration()
        NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) { _, _ in }
    }
}
