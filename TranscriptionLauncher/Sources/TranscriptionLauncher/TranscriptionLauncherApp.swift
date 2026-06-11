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
    /// Files opened from Finder ("Open With", double-click) can arrive
    /// before the SwiftUI scene attaches its handler; the buffer bridges
    /// that gap.
    let openedFiles = OpenURLBuffer()

    func application(_ application: NSApplication, open urls: [URL]) {
        NSApp.activate()
        openedFiles.deliver(urls)
    }

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
            .onAppear {
                // Strong capture is deliberate: the model must stay reachable
                // for as long as the delegate can deliver opened files, and no
                // cycle exists because the model never references the delegate.
                appDelegate.openedFiles.handler = { urls in
                    launcherModel.acceptDroppedFiles(urls)
                }
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
