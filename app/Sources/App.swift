import AppKit
import ServiceManagement

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {

    private let endpoints = Endpoints(port: Endpoints.resolvePort())
    private var client: BrainClient!
    private var supervisor: BrainSupervisor!
    private var monitor: StateMonitor!
    private var panel: MenuPanelController!
    private var commandCenter: CommandCenterController!
    private var permissionsWC: PermissionsWindowController?
    private var statusItem: NSStatusItem!
    private let contextMenu = NSMenu()
    private var signalSources: [DispatchSourceSignal] = []

    private var brainState = "idle"
    private var muted = false
    private var hotKeyBinding: HotKeyManager.Binding = .none
    private var iconTimer: Timer?
    private var iconPhase: CGFloat = 0
    private var launchedAtLogin = false

    // MARK: - Lifecycle

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Only readable while the launch event is being handled.
        if let event = NSAppleEventManager.shared().currentAppleEvent {
            launchedAtLogin = event.eventID == kAEOpenApplication
                && event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMainMenu()

        client = BrainClient(endpoints: endpoints)
        panel = MenuPanelController(endpoints: endpoints)
        commandCenter = CommandCenterController(endpoints: endpoints)
        commandCenter.onRestartBrain = { [weak self] in self?.menuRestart() }
        commandCenter.onPermissions = { [weak self] in self?.menuPermissions() }
        panel.onOpenCommandCenter = { [weak self] in
            self?.panel.hide()
            self?.commandCenter.show()
        }

        buildStatusItem()

        hotKeyBinding = HotKeyManager.shared.install()
        HotKeyManager.shared.onPress = { [weak self] in self?.activateBrain() }

        supervisor = BrainSupervisor(client: client)
        supervisor.onFatal = { [weak self] title, message in
            self?.alert(title: title, message: message)
        }
        supervisor.onRespawn = { [weak self] in
            self?.panel.reload()
            self?.commandCenter.reload()
        }

        monitor = StateMonitor(url: endpoints.ws)
        monitor.onState = { [weak self] state in
            MainActor.assumeIsolated { self?.handleState(state) }
        }

        installSignalHandlers()
        supervisor.start()
        monitor.start()

        client.status { [weak self] status in
            MainActor.assumeIsolated {
                guard let self, let status else { return }
                if let m = status.muted { self.muted = m }
                if let s = status.state { self.handleState(s) }
            }
        }

        enableLoginItemOnce()
        PermissionsWindowController.requestMicrophoneOnFirstLaunch()

        // Opened by hand (Finder, Spotlight, Launchpad, Dock): the Command Center.
        // Started at login: stay silent in the menu bar.
        if !launchedAtLogin {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                self?.commandCenter.show()
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    /// Launching Jarvis again while it runs (Spotlight, Finder, a Dock click) opens the Command Center.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        panel.hide()
        commandCenter.show()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        monitor.stop()
        supervisor.shutdown()
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }

    /// `pkill` sends SIGTERM, which AppKit does not turn into a normal quit -
    /// without this the app dies and leaves the brain holding the microphone.
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                MainActor.assumeIsolated {
                    self?.supervisor.shutdown()
                    NSApp.terminate(nil)
                }
            }
            source.resume()
            signalSources.append(source)
        }
    }

    /// Always listening means running from login. Turned on once; if the user
    /// switches it off from the menu, that choice sticks.
    private func enableLoginItemOnce() {
        let key = "JarvisLoginItemConfigured"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        UserDefaults.standard.set(true, forKey: key)
        try? SMAppService.mainApp.register()
    }

    // MARK: - Brain state

    private func handleState(_ state: String) {
        brainState = state
        refreshIcon()
        let animated = ["listening", "thinking", "working", "speaking", "confirm"].contains(state)
        if animated, iconTimer == nil {
            iconTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 14, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.iconPhase += 0.45
                    self.refreshIcon()
                }
            }
        } else if !animated {
            iconTimer?.invalidate()
            iconTimer = nil
            iconPhase = 0
        }
    }

    private func refreshIcon() {
        let state = muted ? "muted" : brainState
        statusItem.button?.image = StatusIcon.image(state: state, phase: iconPhase)
        statusItem.button?.toolTip = "Jarvis — \(state)"
    }

    private func activateBrain() {
        client.activate { [weak self] ok in
            MainActor.assumeIsolated {
                guard let self, !ok else { return }
                self.panel.show(relativeTo: self.statusItem.button)   // it explains why
            }
        }
    }

    // MARK: - Menu bar

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = statusItem.button else { return }
        button.image = StatusIcon.image(state: "idle", phase: 0)
        button.toolTip = "Jarvis"
        button.target = self
        button.action = #selector(statusClicked(_:))
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        contextMenu.delegate = self
    }

    /// Left click: the panel. Right click (or control-click): the menu.
    @objc private func statusClicked(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        let wantsMenu = event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true
        if wantsMenu {
            panel.hide()
            statusItem.menu = contextMenu
            sender.performClick(nil)
        } else {
            panel.toggle(relativeTo: sender)
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) { rebuild(menu) }

    func menuDidClose(_ menu: NSMenu) {
        statusItem.menu = nil   // otherwise every left click opens the menu too
    }

    private func rebuild(_ menu: NSMenu) {
        menu.removeAllItems()

        let center = NSMenuItem(title: "Open Command Center", action: #selector(menuCommandCenter), keyEquivalent: "")
        center.target = self
        menu.addItem(center)
        menu.addItem(.separator())

        let talk = NSMenuItem(title: "Talk to Jarvis", action: #selector(menuTalk), keyEquivalent: "")
        talk.target = self
        if hotKeyBinding != .none {
            talk.keyEquivalent = "\\"
            talk.keyEquivalentModifierMask = hotKeyBinding.modifierMask
        }
        menu.addItem(talk)

        let mute = NSMenuItem(title: "Mute microphone", action: #selector(menuMute), keyEquivalent: "")
        mute.target = self
        mute.state = muted ? .on : .off
        menu.addItem(mute)

        menu.addItem(.separator())

        let health = NSMenuItem(title: supervisor.summary, action: nil, keyEquivalent: "")
        health.isEnabled = false
        menu.addItem(health)

        let restart = NSMenuItem(title: "Restart brain", action: #selector(menuRestart), keyEquivalent: "")
        restart.target = self
        menu.addItem(restart)

        let perms = NSMenuItem(title: "Permissions…", action: #selector(menuPermissions), keyEquivalent: "")
        perms.target = self
        menu.addItem(perms)

        let log = NSMenuItem(title: "Open log", action: #selector(menuLog), keyEquivalent: "")
        log.target = self
        menu.addItem(log)

        let login = NSMenuItem(title: "Start at login", action: #selector(menuStartAtLogin), keyEquivalent: "")
        login.target = self
        login.state = (SMAppService.mainApp.status == .enabled) ? .on : .off
        menu.addItem(login)

        menu.addItem(.separator())

        let quit = NSMenuItem(title: "Quit Jarvis", action: #selector(menuQuit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
    }

    @objc private func menuShow() { panel.show(relativeTo: statusItem.button) }

    @objc private func menuCommandCenter() {
        panel.hide()
        commandCenter.show()
    }

    @objc private func menuTalk() { activateBrain() }

    @objc private func menuMute() {
        let next = !muted
        muted = next
        refreshIcon()
        client.setMuted(next) { [weak self] ok in
            MainActor.assumeIsolated {
                guard let self, !ok else { return }
                self.muted = !next          // the brain never heard it; do not lie
                self.refreshIcon()
                self.alert(title: "Jarvis couldn't change the microphone",
                           message: "The brain did not answer on port \(self.endpoints.port).")
            }
        }
    }

    @objc private func menuRestart() {
        if supervisor.adopted {
            alert(title: "This brain isn't Jarvis's to restart",
                  message: "Something was already answering on port \(endpoints.port) when "
                         + "Jarvis launched, so it adopted that process instead of starting "
                         + "its own. Quit it where you started it, then use Restart brain again.")
            supervisor.start()
            return
        }
        supervisor.restart()
        panel.reload()
    }

    @objc private func menuLog() { Logs.open() }

    @objc private func menuPermissions() {
        if permissionsWC == nil { permissionsWC = PermissionsWindowController() }
        permissionsWC?.present()
    }

    @objc private func menuStartAtLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            alert(title: "Jarvis couldn't change the login item", message: error.localizedDescription)
        }
    }

    @objc private func menuQuit() { NSApp.terminate(nil) }

    // MARK: - Main menu

    /// Accessory apps still route ⌘C/⌘V/⌘A through the main menu while active,
    /// so the command field needs an Edit menu to copy and paste at all.
    private func buildMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        let center = NSMenuItem(title: "Open Command Center", action: #selector(menuCommandCenter), keyEquivalent: "0")
        center.target = self
        appMenu.addItem(center)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Jarvis", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Jarvis", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(redo)
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        let full = NSMenuItem(title: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        full.keyEquivalentModifierMask = [.command, .control]
        viewMenu.addItem(full)
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    // MARK: - Alerts

    private func alert(title: String, message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Open Log")
        if alert.runModal() == .alertSecondButtonReturn {
            Logs.open()
        }
    }
}

/// The menu bar glyph: a sphere sketched out of dots that moves with Jarvis's
/// state, so you can tell it heard you without opening anything. Drawn rather
/// than shipped as an asset so it stays crisp and follows the menu bar tint.
enum StatusIcon {

    @MainActor
    static func image(state: String, phase: CGFloat) -> NSImage {
        let side: CGFloat = 18
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
            let c = CGPoint(x: rect.midX, y: rect.midY)
            let muted = state == "muted"
            NSColor.black.withAlphaComponent(muted ? 0.45 : 1).setFill()

            func dot(_ x: CGFloat, _ y: CGFloat, _ r: CGFloat) {
                NSBezierPath(ovalIn: NSRect(x: x - r, y: y - r, width: r * 2, height: r * 2)).fill()
            }

            var radius: CGFloat = 7.4
            switch state {
            case "listening": radius = 7.0 + 0.8 * (0.5 + 0.5 * sin(phase))   // breathing
            case "speaking":  radius = 7.2
            default: break
            }

            // Silhouette. Speaking ripples it; everything else keeps it round.
            for i in 0..<12 {
                let a = CGFloat(i) / 12 * .pi * 2
                var r = radius
                if state == "speaking" { r += 0.9 * sin(phase * 1.6 + CGFloat(i) * 1.1) }
                dot(c.x + cos(a) * r, c.y + sin(a) * r, 0.85)
            }

            // Latitude rings. Thinking spins them.
            let spin = (state == "thinking" || state == "working") ? phase * 0.5 : 0
            for (offset, count) in [(CGFloat(0), 8), (CGFloat(0.45), 6), (CGFloat(-0.45), 6)] {
                let ry = radius * offset
                let rx = radius * sqrt(max(0, 1 - offset * offset))
                for i in 0..<count {
                    let a = CGFloat(i) / CGFloat(count) * .pi * 2 + spin
                    dot(c.x + cos(a) * rx, c.y + ry + sin(a) * rx * 0.26, 0.7)
                }
            }

            // Listening: a solid core, so "it heard me" reads at a glance.
            if state == "listening" || state == "confirm" { dot(c.x, c.y, 2.3) }

            if muted {
                let slash = NSBezierPath()
                slash.move(to: CGPoint(x: 2.5, y: 2.5))
                slash.line(to: CGPoint(x: side - 2.5, y: side - 2.5))
                slash.lineWidth = 1.6
                NSColor.black.setStroke()
                slash.stroke()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}

/// `@main` rather than a main.swift: top-level code is nonisolated and cannot build
/// a @MainActor delegate.
@main
struct JarvisApp {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)   // menu bar only: no Dock icon, no app switcher
        app.run()
    }
}
