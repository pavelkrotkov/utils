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
        NSApp.activate()

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
    @StateObject private var onboardingState = OnboardingState()

    var body: some Scene {
        WindowGroup {
            Group {
                if onboardingState.isComplete {
                    MainView(
                        repoRootStore: repoRootStore,
                        model: launcherModel,
                        runner: launcherModel.runner
                    )
                } else {
                    OnboardingView(repoRootStore: repoRootStore) {
                        onboardingState.markComplete()
                    }
                }
            }
            // Files opened from Finder ("Open With", double-click) arrive
            // here once the document types in Info.plist are registered;
            // SwiftUI reopens or creates the window as needed.
            .onOpenURL { url in
                launcherModel.acceptInputFiles([url])
            }
        }
        .commands {
            CommandGroup(after: .help) {
                Button("Run Setup Again") {
                    onboardingState.restart()
                }
            }
        }
        Settings {
            SettingsView(
                repoRootStore: repoRootStore,
                model: launcherModel,
                onboardingState: onboardingState
            )
        }
    }
}
