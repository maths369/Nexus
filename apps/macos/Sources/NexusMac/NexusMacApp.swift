import AppKit
import SwiftUI

@main
struct NexusMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()
    @StateObject private var windows = WindowCoordinator()

    var body: some Scene {
        MenuBarExtra("Nexus", systemImage: model.statusIconName) {
            MenuBarContentView(model: model, windows: windows)
                .environment(\.colorScheme, .dark)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}
