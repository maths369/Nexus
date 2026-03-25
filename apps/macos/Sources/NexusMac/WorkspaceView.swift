import SwiftUI
import WebKit

struct WorkspaceView: View {
    var body: some View {
        WorkspaceWebView()
            .frame(
                minWidth: 1000, idealWidth: 1400, maxWidth: .infinity,
                minHeight: 600, idealHeight: 900, maxHeight: .infinity
            )
    }
}

struct WorkspaceWebView: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        config.preferences.setValue(true, forKey: "allowFileAccessFromFileURLs")
        config.setValue(true, forKey: "allowUniversalAccessFromFileURLs")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.setValue(false, forKey: "drawsBackground")
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator

        loadContent(webView, coordinator: context.coordinator)
        return webView
    }

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func updateNSView(_ webView: WKWebView, context: Context) {}

    private func loadContent(_ webView: WKWebView, coordinator: Coordinator) {
        let settings = SidecarSettings.load()
        let sidecarUI = "http://\(settings.localHost):\(settings.localPort)/"
        coordinator.targetURL = URL(string: sidecarUI)
        coordinator.maxRetries = 15      // retry up to 15 times (≈15s)
        coordinator.retryInterval = 1.0  // 1 second between retries
        coordinator.retryCount = 0

        if let url = coordinator.targetURL {
            webView.load(URLRequest(url: url))
        }
    }

    /// Coordinator handles navigation failures — retries loading while Sidecar starts up.
    class Coordinator: NSObject, WKNavigationDelegate {
        var targetURL: URL?
        var maxRetries = 15
        var retryInterval: TimeInterval = 1.0
        var retryCount = 0

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            retryIfNeeded(webView)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            retryIfNeeded(webView)
        }

        private func retryIfNeeded(_ webView: WKWebView) {
            guard retryCount < maxRetries, let url = targetURL else { return }
            retryCount += 1
            DispatchQueue.main.asyncAfter(deadline: .now() + retryInterval) {
                webView.load(URLRequest(url: url))
            }
        }
    }
}

extension WorkspaceWebView.Coordinator: WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @Sendable () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @Sendable (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @Sendable (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")

        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.stringValue = defaultText ?? ""
        alert.accessoryView = input

        let response = alert.runModal()
        completionHandler(response == .alertFirstButtonReturn ? input.stringValue : nil)
    }
}
