import Foundation

/// Fixed locations. The Python brain is launched from a checkout on disk rather
/// than from inside the bundle: the .app is a thin shell that gets rebuilt often,
/// while the brain is edited in place and must keep its own state.
enum Paths {
    /// The checkout this app was built from (build.sh records it in Info.plist),
    /// overridable with JARVIS_ROOT. The TCC grants the brain inherits are tied to
    /// the app bundle, not to this path.
    static let root: String =
        ProcessInfo.processInfo.environment["JARVIS_ROOT"]
        ?? (Bundle.main.object(forInfoDictionaryKey: "JarvisRoot") as? String)
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("jarvis").path

    static var brainDir: String { root + "/brain" }
    static var python: String { brainDir + "/.venv/bin/python" }

    static var home: URL { FileManager.default.homeDirectoryForCurrentUser }
    static var jarvisHome: URL {
        if let override = ProcessInfo.processInfo.environment["JARVIS_HOME"] {
            return URL(fileURLWithPath: override)
        }
        return home.appendingPathComponent(".jarvis", isDirectory: true)
    }
    static var logsDir: URL { jarvisHome.appendingPathComponent("logs", isDirectory: true) }
    static var brainLog: URL { logsDir.appendingPathComponent("brain.log") }
    static var brainLogPrevious: URL { logsDir.appendingPathComponent("brain.log.1") }
    static var configFile: URL { jarvisHome.appendingPathComponent("config.json") }
}

/// Every URL the shell talks to. Resolved once at launch: the port is read from
/// the brain's own config file so the two halves cannot disagree.
struct Endpoints {
    let port: Int

    /// ~/.jarvis/config.json stores overrides only, so a missing file or a missing
    /// key both mean "the default", exactly as the Python side treats it.
    static func resolvePort() -> Int {
        guard let data = try? Data(contentsOf: Paths.configFile),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let server = root["server"] as? [String: Any],
              let port = server["port"] as? Int,
              (1...65535).contains(port)
        else { return 8765 }
        return port
    }

    private var origin: String { "http://127.0.0.1:\(port)" }

    var ui: URL { URL(string: origin + "/")! }
    var panelUI: URL { URL(string: origin + "/?panel=1")! }
    var status: URL { URL(string: origin + "/api/status")! }
    var activate: URL { URL(string: origin + "/api/activate")! }
    var mute: URL { URL(string: origin + "/api/listening")! }
    var ws: URL { URL(string: "ws://127.0.0.1:\(port)/ws")! }
}

/// What GET /api/status told us. Everything but `ok` is optional: the shell has to
/// come up cleanly against a brain that is older, newer, or half-initialised.
struct BrainStatus {
    var state: String?
    var muted: Bool?
}
