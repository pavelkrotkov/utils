import Foundation

/// Holds file URLs delivered by Launch Services (Finder's "Open With",
/// double-click) until the UI is ready to receive them.
///
/// `application(_:open:)` can fire during launch, before the SwiftUI scene
/// has appeared and attached its handler. URLs that arrive in that window
/// are buffered and forwarded, in delivery order, as soon as a handler is
/// set; later deliveries go straight to the handler.
@MainActor
public final class OpenURLBuffer {
    /// The downstream receiver. Setting it flushes any buffered URLs.
    public var handler: (([URL]) -> Void)? {
        didSet { flush() }
    }

    private var pending: [URL] = []

    public init() {}

    /// Forwards `urls` to the handler, or buffers them when none is set yet.
    public func deliver(_ urls: [URL]) {
        guard !urls.isEmpty else {
            return
        }
        if let handler {
            handler(urls)
        } else {
            pending.append(contentsOf: urls)
        }
    }

    private func flush() {
        guard let handler, !pending.isEmpty else {
            return
        }
        let urls = pending
        pending = []
        handler(urls)
    }
}
