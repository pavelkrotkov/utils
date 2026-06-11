import AppKit
import OSLog
import SwiftUI
import TranscriptionLauncherLib

private let environmentLogger = Logger(
    subsystem: "com.pavelkrotkov.TranscriptionLauncher",
    category: "environment"
)

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)

        // Warm the snapshot cache so the first run doesn't pay for a login
        // shell launch.
        Task {
            do {
                _ = try await EnvironmentSnapshot.capture()
            } catch {
                environmentLogger.warning(
                    "Unable to capture login shell environment: \(String(describing: error), privacy: .public)"
                )
            }
        }
    }
}

@main
@MainActor
struct TranscriptionLauncherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var repoRootStore = RepoRootStore()
    @StateObject private var launcherModel = LauncherModel()

    var body: some Scene {
        WindowGroup {
            MainView(
                repoRootStore: repoRootStore,
                model: launcherModel,
                runner: launcherModel.runner
            )
        }
        Settings {
            SettingsView(repoRootStore: repoRootStore, model: launcherModel)
        }
    }
}
