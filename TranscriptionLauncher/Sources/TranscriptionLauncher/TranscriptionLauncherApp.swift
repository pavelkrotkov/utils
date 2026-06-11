import SwiftUI
import TranscriptionLauncherLib
import AppKit
import OSLog

private let environmentLogger = Logger(
    subsystem: "com.pavelkrotkov.TranscriptionLauncher",
    category: "environment"
)

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)

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
struct TranscriptionLauncherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

private struct ContentView: View {
    private let metadata = AppMetadata()

    var body: some View {
        Text(metadata.displayName)
            .frame(minWidth: 360, minHeight: 180)
    }
}
