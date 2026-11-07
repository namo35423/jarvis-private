import AppKit

/// Owns the Python process.
///
/// The brain runs as a child of this app on purpose: TCC attributes a child's
/// microphone, Apple Events and screen recording use to the *responsible* process,
/// which is this bundle. That is why the bundle id and install path must stay put,
/// and why the app - not the terminal - is the normal way to run Jarvis.
@MainActor
final class BrainSupervisor {

    enum Health {
        case starting
        case running(pid: Int32)
        case adopted          // someone else's brain answered on the port first
        case waiting(seconds: Int)
        case stopped
    }

    private(set) var health: Health = .starting
    private(set) var adopted = false

    /// Fired whenever `health` changes, so the menu can redraw.
    var onChange: (() -> Void)?
    /// Fired for things the user has to see: a missing venv, or five deaths in a row.
    var onFatal: ((String, String) -> Void)?
    /// Fired after a successful respawn so the web view can reload.
    var onRespawn: (() -> Void)?

    private let client: BrainClient
    private var process: Process?
    private var logHandle: FileHandle?
    private var startedAt: Date?
    private var failures = 0
    private var alerted = false
    private var shuttingDown = false
    private var suppressRestart = false
    private var pendingStart: DispatchWorkItem?
    private var everSpawned = false

    private let backoff: [TimeInterval] = [1, 2, 4, 8, 16, 30]
    private let maxLogBytes: UInt64 = 5 * 1024 * 1024

    init(client: BrainClient) {
        self.client = client
    }

    // MARK: - Lifecycle

    /// Probe before spawning: a brain already answering on the port is almost always
    /// one the user started in a terminal to watch it log. Two brains would fight
    /// over the microphone, so we adopt instead.
    func start() {
        health = .starting
        onChange?()
        client.status { [weak self] status in
            MainActor.assumeIsolated {
                guard let self, !self.shuttingDown else { return }
                if status != nil {
                    self.adopted = true
                    self.health = .adopted
                    self.onChange?()
                } else {
                    self.spawn()
                }
            }
        }
    }

    func restart() {
        pendingStart?.cancel()
        pendingStart = nil
        failures = 0
        alerted = false
        if let p = process, p.isRunning {
            suppressRestart = true
            terminate(p, graceSeconds: 3)
            process = nil
            suppressRestart = false
        }
        closeLog()
        adopted = false
        spawn()
    }

    /// Called from applicationWillTerminate and from the SIGTERM handler. Blocking
    /// the main thread here is the point: the app must not exit before the child is
    /// gone, or the microphone stays held by an orphan.
    func shutdown() {
        shuttingDown = true
        pendingStart?.cancel()
        pendingStart = nil
        if let p = process, p.isRunning {
            terminate(p, graceSeconds: 3)
        }
        process = nil
        closeLog()
        health = .stopped
    }

    var isManaging: Bool { process?.isRunning == true }

    var summary: String {
        switch health {
        case .starting:            return "Brain: starting…"
        case .running(let pid):    return "Brain: running (pid \(pid))"
        case .adopted:             return "Brain: adopted — started outside Jarvis"
        case .waiting(let secs):   return "Brain: restarting in \(secs)s"
        case .stopped:             return "Brain: not running"
        }
    }

    // MARK: - Spawning

    private func spawn() {
        guard !shuttingDown, process == nil else { return }

        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: Paths.python) else {
            health = .stopped
            onChange?()
            onFatal?("Jarvis can't find its Python environment",
                     "Expected an interpreter at:\n\(Paths.python)\n\n"
                     + "Create the virtualenv in \(Paths.brainDir) and use Restart brain.")
            return
        }

        try? fm.createDirectory(at: Paths.logsDir, withIntermediateDirectories: true)
        rotateLogIfNeeded()
        let handle = openLog()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: Paths.python)
        p.arguments = ["-m", "jarvis"]
        p.currentDirectoryURL = URL(fileURLWithPath: Paths.brainDir)

        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        // A GUI launch inherits almost no PATH; the brain shells out to system
        // tools (osascript, pmset, ollama) and needs the usual places.
        let extra = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin", Paths.home.appendingPathComponent("bin").path]
        let existing = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        env["PATH"] = (existing + extra.filter { !existing.contains($0) }).joined(separator: ":")
        p.environment = env

        if let handle {
            p.standardOutput = handle
            p.standardError = handle
            handle.write(Data("\n===== jarvis brain starting \(Self.timestamp()) =====\n".utf8))
        }
        p.standardInput = FileHandle.nullDevice

        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { [weak self] in
                MainActor.assumeIsolated { self?.handleExit(proc) }
            }
        }

        do {
            try p.run()
        } catch {
            health = .stopped
            onChange?()
            onFatal?("Jarvis couldn't start its brain",
                     "\(Paths.python) -m jarvis failed to launch:\n\(error.localizedDescription)")
            return
        }

        process = p
        startedAt = Date()
        health = .running(pid: p.processIdentifier)
        onChange?()
        if everSpawned { onRespawn?() }
        everSpawned = true
    }

    private func handleExit(_ proc: Process) {
        guard process === proc else { return }
        process = nil
        closeLog()
        guard !shuttingDown, !suppressRestart else { return }

        // A brain that ran for a while and then died is a fresh incident, not part
        // of a crash loop - otherwise one bad week of uptime would exhaust the budget.
        let ranFor = Date().timeIntervalSince(startedAt ?? Date())
        if ranFor > 60 {
            failures = 0
            alerted = false
        }
        failures += 1

        let delay = backoff[min(failures - 1, backoff.count - 1)]
        health = .waiting(seconds: Int(delay))
        onChange?()

        if failures >= 5 && !alerted {
            alerted = true
            onFatal?("The Jarvis brain keeps stopping",
                     "It exited \(failures) times in a row (last exit code "
                     + "\(proc.terminationStatus)). Jarvis will keep retrying every 30 seconds.\n\n"
                     + "The log is at \(Paths.brainLog.path).")
        }

        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingStart = nil
            self.spawn()
        }
        pendingStart = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    /// SIGTERM, then SIGKILL if it is still there. Python gets a chance to release
    /// the audio device and flush its database; after three seconds it loses it.
    private func terminate(_ p: Process, graceSeconds: TimeInterval) {
        p.terminate()
        let deadline = Date().addingTimeInterval(graceSeconds)
        while p.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if p.isRunning {
            kill(p.processIdentifier, SIGKILL)
            let hardDeadline = Date().addingTimeInterval(1)
            while p.isRunning && Date() < hardDeadline {
                usleep(50_000)
            }
        }
    }

    // MARK: - Log file

    private func openLog() -> FileHandle? {
        let fm = FileManager.default
        if !fm.fileExists(atPath: Paths.brainLog.path) {
            fm.createFile(atPath: Paths.brainLog.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: Paths.brainLog) else { return nil }
        _ = try? handle.seekToEnd()
        logHandle = handle
        return handle
    }

    private func closeLog() {
        try? logHandle?.close()
        logHandle = nil
    }

    private func rotateLogIfNeeded() {
        let fm = FileManager.default
        guard let attrs = try? fm.attributesOfItem(atPath: Paths.brainLog.path),
              let size = attrs[.size] as? UInt64, size > maxLogBytes else { return }
        try? fm.removeItem(at: Paths.brainLogPrevious)
        try? fm.moveItem(at: Paths.brainLog, to: Paths.brainLogPrevious)
    }

    private static func timestamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.string(from: Date())
    }
}
