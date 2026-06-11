import SwiftUI
import TranscriptionLauncherLib
import AppKit
import Combine
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
@MainActor
struct TranscriptionLauncherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var repoRootStore = RepoRootStore()

    var body: some Scene {
        WindowGroup {
            ContentView(repoRootStore: repoRootStore)
        }
        Settings {
            SettingsView(repoRootStore: repoRootStore)
        }
    }
}

private struct ContentView: View {
    private let metadata = AppMetadata()
    @ObservedObject var repoRootStore: RepoRootStore
    @State private var didPromptForRepoRoot = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(metadata.displayName)
                .font(.headline)

            repoRootSummary
        }
        .padding()
        .frame(minWidth: 420, minHeight: 180, alignment: .leading)
        .onAppear {
            guard !didPromptForRepoRoot else {
                return
            }

            didPromptForRepoRoot = true
            repoRootStore.promptForRepoRootIfNeeded()
        }
    }

    @ViewBuilder
    private var repoRootSummary: some View {
        if let repoRootURL = repoRootStore.repoRootURL {
            LabeledContent("Repository Root", value: repoRootURL.path)
        } else {
            Text("Repository root is not configured.")
                .foregroundStyle(.secondary)
        }
    }
}

private struct SettingsView: View {
    @ObservedObject var repoRootStore: RepoRootStore

    var body: some View {
        Form {
            LabeledContent("Repository Root") {
                HStack(spacing: 8) {
                    Text(repoRootStore.repoRootURL?.path ?? "Not configured")
                        .foregroundStyle(repoRootStore.repoRootURL == nil ? .secondary : .primary)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Button("Change...") {
                        repoRootStore.chooseRepoRoot()
                    }
                }
            }
        }
        .padding()
        .frame(minWidth: 520, minHeight: 120)
    }
}

@MainActor
private final class RepoRootStore: ObservableObject {
    @Published private(set) var repoRootURL: URL?

    private let defaults: UserDefaults

    init(
        defaults: UserDefaults = .standard,
        detectorStartURL: URL = Bundle.main.bundleURL
    ) {
        self.defaults = defaults
        self.repoRootURL = Self.loadRepoRoot(defaults: defaults)
            ?? RepoDetector.findRepoRoot(startingFrom: detectorStartURL)

        if let repoRootURL {
            save(repoRootURL)
        }
    }

    func promptForRepoRootIfNeeded() {
        guard repoRootURL == nil else {
            return
        }

        chooseRepoRoot()
    }

    func chooseRepoRoot() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.title = "Choose Repository Root"
        panel.prompt = "Choose"
        panel.directoryURL = repoRootURL

        guard panel.runModal() == .OK, let selectedURL = panel.url else {
            return
        }

        save(selectedURL)
    }

    private func save(_ url: URL) {
        let standardizedURL = url.standardizedFileURL
        repoRootURL = standardizedURL
        defaults.set(standardizedURL.path, forKey: DefaultsKeys.repoRootPath)
    }

    private static func loadRepoRoot(defaults: UserDefaults) -> URL? {
        guard let savedPath = defaults.string(forKey: DefaultsKeys.repoRootPath),
              !savedPath.isEmpty else {
            return nil
        }

        return URL(fileURLWithPath: savedPath, isDirectory: true).standardizedFileURL
    }
}

private enum DefaultsKeys {
    static let repoRootPath = "repoRootPath"
}
