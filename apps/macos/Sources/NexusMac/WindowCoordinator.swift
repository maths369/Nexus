import AppKit
import SwiftUI

enum NexusWindowKind {
    case dashboard
    case meshTrace
    case settings

    var title: String {
        switch self {
        case .dashboard:
            return "Nexus"
        case .meshTrace:
            return "Nexus Mesh Trace"
        case .settings:
            return "Nexus Settings"
        }
    }

    var autosaveName: String {
        switch self {
        case .dashboard:
            return "NexusDashboardWindow.v2"
        case .meshTrace:
            return "NexusMeshTraceWindow.v1"
        case .settings:
            return "NexusSettingsWindow.v2"
        }
    }

    var preferredContentSize: NSSize {
        switch self {
        case .dashboard:
            return NSSize(width: 860, height: 700)
        case .meshTrace:
            return NSSize(width: 1180, height: 820)
        case .settings:
            return NSSize(width: 960, height: 860)
        }
    }

    var minimumContentSize: NSSize {
        switch self {
        case .dashboard:
            return NSSize(width: 760, height: 640)
        case .meshTrace:
            return NSSize(width: 980, height: 720)
        case .settings:
            return NSSize(width: 900, height: 780)
        }
    }
}

enum WindowLayoutPlan {
    static func needsReset(frame: NSRect, visibleFrame: NSRect, minimumContentSize: NSSize) -> Bool {
        let contentSize = frame.size
        let tooSmall = contentSize.width < minimumContentSize.width || contentSize.height < minimumContentSize.height
        let outsideVisibleFrame = !visibleFrame.intersects(frame)
        let notPlacedYet = frame.origin == .zero
        return tooSmall || outsideVisibleFrame || notPlacedYet
    }

    static func centeredFrame(
        visibleFrame: NSRect,
        preferredContentSize: NSSize,
        minimumContentSize: NSSize
    ) -> NSRect {
        let width = min(max(preferredContentSize.width, minimumContentSize.width), visibleFrame.width)
        let height = min(max(preferredContentSize.height, minimumContentSize.height), visibleFrame.height)
        let originX = visibleFrame.origin.x + (visibleFrame.width - width) / 2
        let originY = visibleFrame.origin.y + (visibleFrame.height - height) / 2
        return NSRect(x: originX, y: originY, width: width, height: height)
    }
}

@MainActor
final class WindowCoordinator: ObservableObject {
    private var dashboardController: NSWindowController?
    private var meshTraceController: NSWindowController?
    private var settingsController: NSWindowController?

    func showDashboard(model: AppModel) {
        let kind = NexusWindowKind.dashboard
        let controller = dashboardController ?? makeController(kind: kind)
        controller.contentViewController = NSHostingController(
            rootView: DashboardView(
                model: model,
                onOpenSettings: { [weak self] in
                    self?.showSettings(model: model)
                },
                onOpenMeshTrace: { [weak self] in
                    self?.showMeshTrace(model: model)
                }
            )
        )
        dashboardController = controller
        present(controller, kind: kind)
    }

    func showSettings(model: AppModel) {
        let kind = NexusWindowKind.settings
        let controller = settingsController ?? makeController(kind: kind)
        controller.contentViewController = NSHostingController(rootView: SettingsView(model: model))
        settingsController = controller
        present(controller, kind: kind)
    }

    func showMeshTrace(model: AppModel) {
        let kind = NexusWindowKind.meshTrace
        let controller = meshTraceController ?? makeController(kind: kind)
        controller.contentViewController = NSHostingController(rootView: MeshTraceView(model: model))
        meshTraceController = controller
        present(controller, kind: kind)
    }

    private func makeController(kind: NexusWindowKind) -> NSWindowController {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: kind.preferredContentSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = kind.title
        window.setFrameAutosaveName(kind.autosaveName)
        window.isReleasedWhenClosed = false
        window.titleVisibility = .visible
        window.titlebarAppearsTransparent = false
        window.backgroundColor = .clear
        window.isOpaque = false
        window.toolbarStyle = .unified
        window.collectionBehavior = [.moveToActiveSpace]
        window.contentMinSize = kind.minimumContentSize
        window.setContentSize(kind.preferredContentSize)
        return NSWindowController(window: window)
    }

    private func present(_ controller: NSWindowController, kind: NexusWindowKind) {
        NSApp.activate(ignoringOtherApps: true)
        controller.showWindow(nil)
        normalize(controller.window, kind: kind)
        controller.window?.makeKeyAndOrderFront(nil)
        controller.window?.orderFrontRegardless()
    }

    private func normalize(_ window: NSWindow?, kind: NexusWindowKind) {
        guard let window else {
            return
        }

        window.contentMinSize = kind.minimumContentSize
        let visibleFrame = window.screen?.visibleFrame ?? NSScreen.main?.visibleFrame

        guard let visibleFrame else {
            return
        }

        if WindowLayoutPlan.needsReset(
            frame: window.frame,
            visibleFrame: visibleFrame,
            minimumContentSize: kind.minimumContentSize
        ) {
            let centeredFrame = WindowLayoutPlan.centeredFrame(
                visibleFrame: visibleFrame,
                preferredContentSize: kind.preferredContentSize,
                minimumContentSize: kind.minimumContentSize
            )
            window.setFrame(centeredFrame, display: true)
        }
    }
}
