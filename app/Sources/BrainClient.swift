import Foundation

/// Thin HTTP client for the brain's local API.
///
/// The session's delegateQueue is the main queue, so every completion handler
/// already runs on the main thread and callers can hop straight back onto the
/// main actor without another dispatch.
final class BrainClient {

    let endpoints: Endpoints
    private let session: URLSession

    init(endpoints: Endpoints) {
        self.endpoints = endpoints
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 2
        cfg.timeoutIntervalForResource = 4
        cfg.waitsForConnectivity = false
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.httpShouldUsePipelining = true
        session = URLSession(configuration: cfg, delegate: nil, delegateQueue: .main)
    }

    /// nil means "nothing is listening on the port yet".
    func status(completion: @escaping (BrainStatus?) -> Void) {
        var req = URLRequest(url: endpoints.status)
        req.timeoutInterval = 1.2
        session.dataTask(with: req) { data, response, _ in
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                completion(nil)
                return
            }
            var status = BrainStatus()
            if let data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                status.state = obj["state"] as? String
                status.muted = (obj["muted"] as? Bool) ?? (obj["mic_muted"] as? Bool)
            }
            completion(status)
        }.resume()
    }

    /// Same effect as saying the wake word.
    func activate(completion: ((Bool) -> Void)? = nil) {
        post(endpoints.activate, body: [:], completion: completion)
    }

    func setMuted(_ muted: Bool, completion: ((Bool) -> Void)? = nil) {
        post(endpoints.mute, body: ["enabled": !muted], completion: completion)
    }

    private func post(_ url: URL, body: [String: Any], completion: ((Bool) -> Void)?) {
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = (try? JSONSerialization.data(withJSONObject: body)) ?? Data("{}".utf8)
        req.timeoutInterval = 3
        session.dataTask(with: req) { _, response, _ in
            let http = response as? HTTPURLResponse
            completion?(http.map { (200..<300).contains($0.statusCode) } ?? false)
        }.resume()
    }
}

/// Watches ws://127.0.0.1:PORT/ws for the brain's state changes, reconnecting for
/// as long as the app runs. The brain is expected to restart under us, so a closed
/// socket is normal rather than an error worth reporting.
final class StateMonitor {

    private let url: URL
    private let session: URLSession
    private var task: URLSessionWebSocketTask?
    private var retry: TimeInterval = 1
    private var stopped = true
    private var pending: DispatchWorkItem?

    /// Called on the main thread with the brain's latest state name.
    var onState: ((String) -> Void)?
    var onConnected: ((Bool) -> Void)?

    init(url: URL) {
        self.url = url
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 10
        cfg.waitsForConnectivity = false
        session = URLSession(configuration: cfg, delegate: nil, delegateQueue: .main)
    }

    func start() {
        guard stopped else { return }
        stopped = false
        retry = 1
        connect()
    }

    func stop() {
        stopped = true
        pending?.cancel()
        pending = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func connect() {
        guard !stopped else { return }
        task?.cancel(with: .goingAway, reason: nil)
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        receive(on: t)
    }

    private func receive(on t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self, !self.stopped, self.task === t else { return }
            switch result {
            case .success(let message):
                self.retry = 1
                self.onConnected?(true)
                switch message {
                case .string(let text): self.handle(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) { self.handle(text) }
                @unknown default: break
                }
                self.receive(on: t)
            case .failure:
                self.onConnected?(false)
                self.scheduleReconnect()
            }
        }
    }

    private func scheduleReconnect() {
        guard !stopped, pending == nil else { return }
        let delay = retry
        retry = min(retry * 2, 10)
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pending = nil
            self.connect()
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    /// Tolerant on purpose: the brain may send a bare {"state": ...} or wrap it in
    /// an envelope. Anything that is not a state event is ignored, not logged.
    private func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        if let state = obj["state"] as? String {
            onState?(state)
            return
        }
        let kind = (obj["type"] as? String) ?? (obj["event"] as? String)
        guard kind == "state" else { return }
        if let value = obj["value"] as? String {
            onState?(value)
        } else if let payload = obj["data"] as? [String: Any],
                  let state = payload["state"] as? String {
            onState?(state)
        }
    }
}
