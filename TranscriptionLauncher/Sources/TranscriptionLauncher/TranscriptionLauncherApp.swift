import SwiftUI
import TranscriptionLauncherLib

#if os(macOS)
import AppKit
#endif

#if os(macOS)
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
}
#endif

@main
struct TranscriptionLauncherApp: App {
    #if os(macOS)
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    #endif

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
