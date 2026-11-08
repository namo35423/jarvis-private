import AppKit
import ServiceManagement
import WebKit

/// The full-size Command Center: a real window (Dock icon, ⌘-Tab, full screen,
/// standard traffic lights) around the brain-served dashboard at /command.html.
///
/// Jarvis stays a menu-bar app: the Dock icon exists only while this window is
/// open, and closing it tears the web view down so nothing animates in the
/// background. The brain keeps running either way.
@MainActor
final class CommandCenterController: NSObject, NSWindowDelegate, WKNavigationDelegate {

    private let endpoints: Endpoints
    private var window: NSWindow?
    private var web: WKWebView?
    private var retry: DispatchWorkItem?
    private var observers: [NSObjectProtocol] = []

    /// Host actions the page asks for (restart the brain, open the permissions window).
    var onRestartBrain: (() -> Void)?
    var onPermissions: (() -> Void)?

    init(endpoints: Endpoints) {
        self.endpoints = endpoints
        super.init()
        let nc = NotificationCenter.default
        for name in [NSApplication.didBecomeActiveNotification, NSApplication.didResignActiveNotification] {
            observers.append(nc.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.pushVisibility() }
            })
        }
    }

    var isOpen: Bool { window?.isVisible == true }

    func show() {
        if window == nil { build() }
        NSApp.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// The brain restarted: the page reconnects on its own, but reload after a
    /// crash so a stale page never sits there looking alive.
    func reload() {
        guard web != nil else { return }
        load()
    }

    // MARK: - Window

    private func build() {
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        w.title = "JARVIS"
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden
        w.minSize = NSSize(width: 980, height: 660)
        w.collectionBehavior = [.fullScreenPrimary, .managed]
        w.appearance = NSAppearance(named: .darkAqua)
        w.backgroundColor = NSColor(srgbRed: 0.016, green: 0.024, blue: 0.047, alpha: 1)
        w.isReleasedWhenClosed = false
        w.tabbingMode = .disallowed
        w.delegate = self
        if !w.setFrameUsingName("JarvisCommandCenter") { w.center() }
        w.setFrameAutosaveName("JarvisCommandCenter")
        window = w
        attachWebView()
    }

    private func attachWebView() {
        guard let w = window else { return }
        let config = WKWebViewConfiguration()
        let content = config.userContentController
        content.add(WeakScriptHandler(self), name: "jarvisHost")
        // Tell the page it is inside the app (titlebar inset, host-only settings).
        content.addUserScript(WKUserScript(
            source: "window.JARVIS_NATIVE = true; document.documentElement.classList.add('native');",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.suppressesIncrementalRendering = true
        let view = WKWebView(frame: w.contentView?.bounds ?? .zero, configuration: config)
        view.autoresizingMask = [.width, .height]
        view.setValue(false, forKey: "drawsBackground")
        view.navigationDelegate = self
        w.contentView = view
        web = view
        load()
    }

    private func load() {
        retry?.cancel()
        let url = URL(string: "http://127.0.0.1:\(endpoints.port)/command.html")!
        web?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 4))
    }

    /// The brain serves the page, so until it answers show a native holding screen.
    private func showStarting() {
        web?.loadHTMLString(Self.startingHTML, baseURL: nil)
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.load() }
        }
        retry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
    }

    func windowWillClose(_ notification: Notification) {
        // Free the page entirely: zero CPU and memory for the dashboard while closed.
        retry?.cancel()
        web?.configuration.userContentController.removeScriptMessageHandler(forName: "jarvisHost")
        web?.stopLoading()
        web?.removeFromSuperview()
        web = nil
        window?.contentView = NSView()
        window = nil
        // Back to a menu-bar app once no other regular window is up.
        DispatchQueue.main.async {
            if !NSApp.windows.contains(where: { $0.isVisible && $0.styleMask.contains(.titled) && $0.level == .normal }) {
                NSApp.setActivationPolicy(.accessory)
            }
        }
    }

    func windowDidChangeOcclusionState(_ notification: Notification) { pushVisibility() }
    func windowDidMiniaturize(_ notification: Notification) { pushVisibility() }
    func windowDidDeminiaturize(_ notification: Notification) { pushVisibility() }

    /// The page throttles its animation: full rate when frontmost, reduced when
    /// another app is active, stopped when the window cannot be seen.
    private func pushVisibility() {
        guard let w = window, let web else { return }
        let visible = w.occlusionState.contains(.visible) && !w.isMiniaturized
        let active = NSApp.isActive && w.isKeyWindow
        send(["type": "visibility", "visible": visible, "active": active], to: web)
    }

    // MARK: - Page bridge

    fileprivate func receive(_ body: Any) {
        guard let msg = body as? [String: Any], let cmd = msg["cmd"] as? String, let web else { return }
        switch cmd {
        case "hello":
            send(hostInfo(), to: web)
            pushVisibility()
        case "restartBrain":
            onRestartBrain?()
        case "openLogs":
            Logs.open()
        case "permissions":
            onPermissions?()
        case "setLoginItem":
            let want = (msg["enabled"] as? Bool) ?? false
            var failure: String?
            do {
                if want { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                failure = error.localizedDescription
            }
            var info = hostInfo()
            if let failure { info["error"] = failure }
            send(info, to: web)
        case "openURL":
            if let s = msg["url"] as? String, let url = URL(string: s), ["http", "https"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
        case "toggleFullScreen":
            window?.toggleFullScreen(nil)
        default:
            break
        }
    }

    private func hostInfo() -> [String: Any] {
        [
            "type": "host",
            "loginItem": SMAppService.mainApp.status == .enabled,
            "reduceMotion": NSWorkspace.shared.accessibilityDisplayShouldReduceMotion,
            "version": (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "",
        ]
    }

    private func send(_ event: [String: Any], to web: WKWebView) {
        guard let data = try? JSONSerialization.data(withJSONObject: event),
              let json = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.jarvisHostEvent && window.jarvisHostEvent(\(json))", completionHandler: nil)
    }

    // MARK: - Navigation

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showStarting()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showStarting()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        load()
    }

    /// Links leave the app for the real browser; the dashboard never navigates away.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated, url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    private static let startingHTML = """
    <!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;background:#04060c;color:#8fe3ff;font:13px -apple-system,system-ui;
      display:flex;align-items:center;justify-content:center;letter-spacing:.3em;text-transform:uppercase}
    .r{width:120px;height:120px;border-radius:50%;border:1px solid rgba(80,200,255,.35);
      border-top-color:#5fd8ff;animation:s 1.6s linear infinite;margin:0 auto 28px}
    @keyframes s{to{transform:rotate(360deg)}} @media (prefers-reduced-motion:reduce){.r{animation:none}}
    p{text-align:center;margin:0} small{display:block;margin-top:10px;color:#4f6b7c;letter-spacing:.1em;text-transform:none}
    </style></head><body><div><div class="r"></div><p>Starting JARVIS</p>
    <small>กำลังเริ่มระบบ AI…</small></div></body></html>
    """
}

/// WKUserContentController retains its handlers; this keeps the controller free to go.
private final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: CommandCenterController?
    init(_ target: CommandCenterController) { self.target = target }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let body = message.body
        MainActor.assumeIsolated { target?.receive(body) }
    }
}
