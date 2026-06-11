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
@MainActor
final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
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

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        // Extract the path before hopping to the main actor:
        // UNNotificationResponse is not Sendable.
        let outputPath = response.notification.request.content
            .userInfo[Self.outputPathKey] as? String
        await Self.handleClick(outputPath: outputPath)
    }

    private static func handleClick(outputPath: String?) {
        if let outputPath {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: outputPath)])
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
