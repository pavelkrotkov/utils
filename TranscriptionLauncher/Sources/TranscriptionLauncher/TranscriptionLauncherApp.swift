import SwiftUI
import TranscriptionLauncherLib

@main
struct TranscriptionLauncherApp: App {
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
