import AppKit
import AVFoundation
import ApplicationServices
import CoreGraphics

/// The four grants Jarvis needs, and the only reliable ways to ask for them.
///
/// The Python brain inherits this app's TCC identity because it is a child process,
/// so everything is requested here, once, by the bundle the user already trusts.
enum Permission: CaseIterable {
    case microphone, accessibility, screenRecording, automation

    var title: String {
        switch self {
        case .microphone:      return "Microphone"
        case .accessibility:   return "Accessibility"
        case .screenRecording: return "Screen Recording"
        case .automation:      return "Automation"
        }
    }

    var detail: String {
        switch self {
        case .microphone:      return "Hearing the wake word and your commands."
        case .accessibility:   return "Typing and clicking on your behalf."
        case .screenRecording: return "Reading what is on screen when you ask about it."
        case .automation:      return "Controlling Music, Calendar, Messages and friends."
        }
    }

    /// The System Settings pane that owns this grant.
    var settingsURL: URL {
        let anchor: String
        switch self {
        case .microphone:      anchor = "Privacy_Microphone"
        case .accessibility:   anchor = "Privacy_Accessibility"
        case .screenRecording: anchor = "Privacy_ScreenCapture"
        case .automation:      anchor = "Privacy_Automation"
        }
        return URL(string: "x-apple.systempreferences:com.apple.preference.security?\(anchor)")!
    }
}

enum PermissionState {
    case granted, denied, notDetermined, unknown

    var label: String {
        switch self {
        case .granted:       return "Granted"
        case .denied:        return "Not granted"
        case .notDetermined: return "Not asked yet"
        case .unknown:       return "Unknown"
        }
    }

    var color: NSColor {
        switch self {
        case .granted:       return NSColor.systemGreen
        case .denied:        return NSColor.systemOrange
        case .notDetermined: return NSColor.secondaryLabelColor
        case .unknown:       return NSColor.secondaryLabelColor
        }
    }
}

enum PermissionCheck {

    static func state(_ permission: Permission) -> PermissionState {
        switch permission {
        case .microphone:
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized:    return .granted
            case .denied:        return .denied
            case .restricted:    return .denied
            case .notDetermined: return .notDetermined
            @unknown default:    return .unknown
            }
        case .accessibility:
            return AXIsProcessTrusted() ? .granted : .denied
        case .screenRecording:
            return CGPreflightScreenCaptureAccess() ? .granted : .denied
        case .automation:
            return automationState
        }
    }

    /// Automation has no polite query API, so this asks the Apple Event manager
    /// whether we *could* drive System Events, explicitly without prompting. It can
    /// block briefly, so it is refreshed off the main thread and cached here.
    nonisolated(unsafe) private static var automationState: PermissionState = .unknown

    static func refreshAutomation(_ completion: @escaping (PermissionState) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result = probeAutomation()
            automationState = result
            DispatchQueue.main.async { completion(result) }
        }
    }

    private static func probeAutomation() -> PermissionState {
        let bundleID = "com.apple.systemevents"
        var target = AEAddressDesc()
        let bytes = Array(bundleID.utf8)
        let created = bytes.withUnsafeBufferPointer { buf in
            AECreateDesc(typeApplicationBundleID, buf.baseAddress, buf.count, &target)
        }
        guard created == noErr else { return .unknown }
        defer { AEDisposeDesc(&target) }

        switch AEDeterminePermissionToAutomateTarget(&target, typeWildCard, typeWildCard, false) {
        case noErr:
            return .granted
        case OSStatus(-1743):   // errAEEventNotPermitted
            return .denied
        case OSStatus(-1744):   // errAEEventWouldRequireUserConsent
            return .notDetermined
        case OSStatus(-600):    // procNotFound: System Events is simply not running
            return .notDetermined
        default:
            return .unknown
        }
    }
}

/// Small always-available panel. Status is re-read on a timer while it is open,
/// because the user grants these in System Settings, not here.
@MainActor
final class PermissionsWindowController: NSWindowController {

    private var rows: [Permission: (status: NSTextField, button: NSButton)] = [:]
    private var timer: Timer?

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 330),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false)
        super.init(window: window)

        window.title = "Jarvis Permissions"
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false
        window.center()
        buildContent()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    deinit { timer?.invalidate() }

    private func buildContent() {
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 330))

        let intro = NSTextField(wrappingLabelWithString:
            "macOS asks per app. Jarvis runs the voice brain as its own child process, "
            + "so granting these here covers everything it does.")
        intro.font = .systemFont(ofSize: 11)
        intro.textColor = .secondaryLabelColor
        intro.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(intro)

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        for permission in Permission.allCases {
            let row = NSStackView()
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 10
            row.translatesAutoresizingMaskIntoConstraints = false

            let text = NSStackView()
            text.orientation = .vertical
            text.alignment = .leading
            text.spacing = 1

            let name = NSTextField(labelWithString: permission.title)
            name.font = .systemFont(ofSize: 13, weight: .medium)
            let detail = NSTextField(labelWithString: permission.detail)
            detail.font = .systemFont(ofSize: 11)
            detail.textColor = .secondaryLabelColor
            text.addArrangedSubview(name)
            text.addArrangedSubview(detail)

            let status = NSTextField(labelWithString: "—")
            status.font = .systemFont(ofSize: 11, weight: .medium)
            status.alignment = .right

            let button = NSButton(title: "Grant", target: self, action: #selector(act(_:)))
            button.bezelStyle = .rounded
            button.controlSize = .small
            button.tag = Permission.allCases.firstIndex(of: permission) ?? 0

            row.addArrangedSubview(text)
            row.addArrangedSubview(NSView())      // spacer
            row.addArrangedSubview(status)
            row.addArrangedSubview(button)
            row.widthAnchor.constraint(equalToConstant: 420).isActive = true
            status.widthAnchor.constraint(equalToConstant: 92).isActive = true
            button.widthAnchor.constraint(equalToConstant: 82).isActive = true

            rows[permission] = (status, button)
            stack.addArrangedSubview(row)
        }

        NSLayoutConstraint.activate([
            intro.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            intro.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            intro.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.topAnchor.constraint(equalTo: intro.bottomAnchor, constant: 18),
        ])

        window?.contentView = content
    }

    func present() {
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        refresh()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.window?.isVisible == true else { return }
                self.refresh()
            }
        }
    }

    private func refresh() {
        for (permission, row) in rows {
            let state = PermissionCheck.state(permission)
            row.status.stringValue = state.label
            row.status.textColor = state.color
            row.button.title = buttonTitle(for: permission, state: state)
            row.button.isEnabled = state != .granted
        }
        PermissionCheck.refreshAutomation { [weak self] state in
            MainActor.assumeIsolated {
                guard let self, let row = self.rows[.automation] else { return }
                row.status.stringValue = state.label
                row.status.textColor = state.color
                row.button.isEnabled = state != .granted
            }
        }
    }

    private func buttonTitle(for permission: Permission, state: PermissionState) -> String {
        if state == .granted { return "Granted" }
        switch permission {
        case .microphone:      return state == .notDetermined ? "Ask" : "Settings"
        case .screenRecording: return "Ask"
        case .accessibility:   return "Ask"
        case .automation:      return "Settings"
        }
    }

    @objc private func act(_ sender: NSButton) {
        let permission = Permission.allCases[sender.tag]
        let state = PermissionCheck.state(permission)

        switch permission {
        case .microphone:
            if state == .notDetermined {
                AVCaptureDevice.requestAccess(for: .audio) { _ in
                    DispatchQueue.main.async {
                        MainActor.assumeIsolated { self.refresh() }
                    }
                }
            } else {
                open(permission)
            }

        case .accessibility:
            // The prompt only appears once per app identity; afterwards this is a
            // no-op and the Settings pane is the only way through.
            let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
            let trusted = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
            if !trusted { open(permission) }
            refresh()

        case .screenRecording:
            if !CGRequestScreenCaptureAccess() { open(permission) }
            refresh()

        case .automation:
            open(permission)
        }
    }

    private func open(_ permission: Permission) {
        NSWorkspace.shared.open(permission.settingsURL)
    }

    /// Asked once, on the very first launch: a voice assistant that cannot hear is
    /// not worth explaining twice.
    static func requestMicrophoneOnFirstLaunch() {
        // No local "already asked" flag: macOS itself only ever prompts once, and
        // a flag set before the prompt is answered loses the request entirely if
        // the app is quit or relaunched in between.
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined else { return }
        // The prompt is only shown reliably once the app is actually active, so
        // ask on the next runloop pass after activation rather than mid-launch.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            // macOS holds back the TCC prompt for an app that is not frontmost,
            // so on the one launch where we ask, take focus deliberately.
            NSApp.activate(ignoringOtherApps: true)
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                NSLog("Jarvis: microphone access \(granted ? "granted" : "refused")")
            }
        }
    }
}
