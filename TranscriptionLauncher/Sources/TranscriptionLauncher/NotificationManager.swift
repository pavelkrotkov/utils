import AppKit
import Foundation
import OSLog
import UserNotifications

private let notificationLogger = Logger(
    subsystem: "com.pavelkrotkov.TranscriptionLauncher",
    category: "notifications"
)

/// Posts a user notification when a transcription run finishes while the
/// app is in the background, and handles clicks on those notifications:
/// a success notification reveals the output file in Finder, a failure
/// notification brings the app to front.
///
/// Callers decide *whether* to notify (only when the app is not frontmost);
/// this type owns permission, delivery, and click handling.
// The delegate conformance is @preconcurrency: the SDK declares the
// requirements nonisolated but delivers them on the main thread (and marks
// `UNNotificationContent.userInfo` @MainActor), so the main-actor methods
// witness them with a runtime isolation check instead of sending checks.
@MainActor
final class NotificationManager: NSObject, @preconcurrency UNUserNotificationCenterDelegate {
    /// `userInfo` key carrying the output file path of a successful run.
    static let outputPathKey = "outputPath"

    /// nil when running unbundled (such as via `swift run`), where
    /// `UNUserNotificationCenter.current()` traps because there is no app
    /// bundle to register with the notification system.
    private let center: UNUserNotificationCenter?

    override init() {
        center = Bundle.main.bundleIdentifier != nil ? .current() : nil
        super.init()
        center?.delegate = self
    }

    func notifySuccess(output: URL) {
        post(
            title: "Transcription complete",
            body: output.lastPathComponent,
            userInfo: [Self.outputPathKey: output.path(percentEncoded: false)]
        )
    }

    func notifyFailure(message: String) {
        post(title: "Transcription failed", body: message, userInfo: [:])
    }

    private func post(title: String, body: String, userInfo: [String: String]) {
        guard let center else {
            return
        }

        Task {
            do {
                // Prompts the user only on first use; afterwards it
                // resolves immediately from the recorded setting.
                let granted = try await center.requestAuthorization(options: [.alert, .sound])
                guard granted else {
                    return
                }

                let content = UNMutableNotificationContent()
                content.title = title
                content.body = body
                content.sound = .default
                content.userInfo = userInfo

                try await center.add(UNNotificationRequest(
                    identifier: UUID().uuidString,
                    content: content,
                    trigger: nil
                ))
            } catch {
                notificationLogger.warning(
                    "Unable to post notification: \(String(describing: error), privacy: .public)"
                )
            }
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let outputPath = response.notification.request.content
            .userInfo[Self.outputPathKey] as? String
        if let outputPath {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: outputPath)])
        } else {
            bringAppToFront()
        }
    }

    /// Activation alone does not restore a minimized launcher window or
    /// recreate one that was closed while the run kept going, which would
    /// leave the failure alert with no window to appear in.
    private func bringAppToFront() {
        NSApp.activate()
        for window in NSApp.windows where window.isMiniaturized {
            window.deminiaturize(nil)
        }
        if !NSApp.windows.contains(where: { $0.isVisible && $0.canBecomeMain }) {
            // Ask for a reopen exactly as a Dock-icon click would; SwiftUI
            // responds by recreating the WindowGroup window.
            _ = NSApp.delegate?.applicationShouldHandleReopen?(NSApp, hasVisibleWindows: false)
        }
    }
}
