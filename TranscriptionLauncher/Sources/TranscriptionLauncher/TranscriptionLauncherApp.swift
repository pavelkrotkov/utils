import SwiftUI
import TranscriptionLauncherLib

#if os(macOS)
import AppKit
#endif

@main
struct TranscriptionLauncherApp: App {
    #if os(macOS)
    init() {
        NSApplication.shared.setActivationPolicy(.regular)
    }
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
