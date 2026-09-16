import AppKit
import WebKit

/// Borderless panels refuse key status by default, which would leave the
/// command field unable to take typing.
final class DropdownPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// The whole UI: a translucent dropdown hanging off the menu bar icon, the way
/// Control Center does it. Nothing lives on the desktop or in the Dock.
@MainActor
final class MenuPanelController: NSObject, WKNavigationDelegate, NSWindowDelegate {

    private let endpoints: Endpoints
    private let panel: DropdownPanel
    private let web: WKWebView
    private var retryTimer: Timer?
    private var loaded = false
    /// Clicking the menu bar icon while the panel is open first resigns key
    /// (which hides it) and then delivers the click (which would re-show it).
    private var lastHidden = Date.distantPast

    static let size = NSSize(width: 392, height: 600)

    var isVisible: Bool { panel.isVisible }

    init(endpoints: Endpoints) {
        self.endpoints = endpoints

        panel = DropdownPanel(contentRect: NSRect(origin: .zero, size: Self.size),
                              styleMask: [.borderless, .nonactivatingPanel],
                              backing: .buffered, defer: false)
        panel.isFloatingPanel = true
        panel.level = .popUpMenu
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]

        let glass = NSVisualEffectView(frame: NSRect(origin: .zero, size: Self.size))
        glass.material = .hudWindow
        glass.blendingMode = .behindWindow
        glass.state = .active
        glass.appearance = NSAppearance(named: .vibrantDark)
        glass.wantsLayer = true
        glass.layer?.cornerRadius = 20
        glass.layer?.cornerCurve = .continuous
        glass.layer?.masksToBounds = true
        glass.layer?.borderWidth = 0.5
        glass.layer?.borderColor = NSColor.white.withAlphaComponent(0.14).cgColor

        let config = WKWebViewConfiguration()
        config.suppressesIncrementalRendering = true
        web = WKWebView(frame: glass.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")   // let the glass show through

        super.init()

        web.navigationDelegate = self
        glass.addSubview(web)
        panel.contentView = glass
        panel.delegate = self
    }

    // MARK: - Showing

    func toggle(relativeTo button: NSStatusBarButton?) {
        if panel.isVisible {
            hide()
        } else if Date().timeIntervalSince(lastHidden) > 0.25 {
            show(relativeTo: button)
        }
    }

    func show(relativeTo button: NSStatusBarButton?) {
        loadIfNeeded()
        position(under: button)
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        web.evaluateJavaScript("window.jarvisShown && window.jarvisShown()", completionHandler: nil)
    }

    func hide() {
        guard panel.isVisible else { return }
        panel.orderOut(nil)
        lastHidden = Date()
    }

    /// Clicking anywhere else dismisses it, like a menu.
    func windowDidResignKey(_ notification: Notification) {
        hide()
    }

    private func position(under button: NSStatusBarButton?) {
        let size = panel.frame.size
        guard let button, let bar = button.window else {
            if let screen = NSScreen.main?.visibleFrame {
                panel.setFrameOrigin(NSPoint(x: screen.maxX - size.width - 12,
                                             y: screen.maxY - size.height - 8))
            }
            return
        }
        let icon = bar.convertToScreen(button.convert(button.bounds, to: nil))
        let visible = (bar.screen ?? NSScreen.main)?.visibleFrame ?? icon
        var x = icon.midX - size.width / 2
        x = min(max(x, visible.minX + 8), visible.maxX - size.width - 8)
        panel.setFrameOrigin(NSPoint(x: x, y: icon.minY - size.height - 6))
    }

    // MARK: - Loading

    private func loadIfNeeded() {
        guard !loaded else { return }
        load()
    }

    func reload() {
        loaded = false
        load()
    }

    private func load() {
        retryTimer?.invalidate()
        web.load(URLRequest(url: endpoints.panelUI, cachePolicy: .reloadIgnoringLocalCacheData,
                            timeoutInterval: 4))
    }

    /// The brain takes a while to come up; keep knocking until it answers.
    private func scheduleRetry() {
        loaded = false
        retryTimer?.invalidate()
        retryTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.load() }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded = true
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        scheduleRetry()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        scheduleRetry()
    }

    /// Links open in the real browser, never inside the panel.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated,
           url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

enum Logs {
    /// Opens the brain log in Console, or the logs folder if nothing is written yet.
    @MainActor
    static func open() {
        let target = FileManager.default.fileExists(atPath: Paths.brainLog.path) ? Paths.brainLog : Paths.logsDir
        NSWorkspace.shared.open(target)
    }
}
