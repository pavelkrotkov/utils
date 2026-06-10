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
    var body: some View {
        Text("Transcription Launcher")
            .frame(minWidth: 360, minHeight: 180)
    }
}
