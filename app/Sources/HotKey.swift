import AppKit
import Carbon.HIToolbox

/// The global "talk to me" shortcut, via Carbon's RegisterEventHotKey.
///
/// Deliberately not NSEvent.addGlobalMonitorForEvents: that needs Accessibility,
/// and a voice assistant should be reachable before the user has granted anything.
/// Carbon hotkeys need no TCC grant at all.
final class HotKeyManager {

    static let shared = HotKeyManager()

    enum Binding {
        case command          // ⌘\
        case controlOption    // ⌃⌥\
        case none

        var display: String {
            switch self {
            case .command:       return "⌘\\"
            case .controlOption: return "⌃⌥\\"
            case .none:          return "none"
            }
        }

        var modifierMask: NSEvent.ModifierFlags {
            switch self {
            case .command:       return [.command]
            case .controlOption: return [.control, .option]
            case .none:          return []
            }
        }
    }

    private(set) var binding: Binding = .none

    var onPress: (() -> Void)?

    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?

    private init() {}

    /// Returns the binding that actually registered. ⌘\ is first choice; if some
    /// other app already owns it we fall back rather than silently doing nothing.
    @discardableResult
    func install() -> Binding {
        guard binding == .none, ref == nil else { return binding }

        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: OSType(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hkID = EventHotKeyID()
            let err = GetEventParameter(event,
                                        EventParamName(kEventParamDirectObject),
                                        EventParamType(typeEventHotKeyID),
                                        nil,
                                        MemoryLayout<EventHotKeyID>.size,
                                        nil,
                                        &hkID)
            guard err == noErr, hkID.id == 1 else { return noErr }
            DispatchQueue.main.async { HotKeyManager.shared.onPress?() }
            return noErr
        }, 1, &spec, nil, &handler)

        if register(modifiers: UInt32(cmdKey)) {
            binding = .command
        } else if register(modifiers: UInt32(controlKey | optionKey)) {
            binding = .controlOption
        } else {
            binding = .none
        }
        return binding
    }

    private func register(modifiers: UInt32) -> Bool {
        var out: EventHotKeyRef?
        let id = EventHotKeyID(signature: OSType(0x4A_52_56_53), id: 1)  // 'JRVS'
        let status = RegisterEventHotKey(UInt32(kVK_ANSI_Backslash), modifiers, id,
                                         GetApplicationEventTarget(), 0, &out)
        guard status == noErr, out != nil else { return false }
        ref = out
        return true
    }
}
