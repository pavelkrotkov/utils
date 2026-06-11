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
    @StateObject private var onboardingState = OnboardingState()

    var body: some Scene {
        WindowGroup {
            if onboardingState.isComplete {
                ContentView(repoRootStore: repoRootStore)
            } else {
                OnboardingView(repoRootStore: repoRootStore) {
                    onboardingState.markComplete()
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
            SettingsView(repoRootStore: repoRootStore, onboardingState: onboardingState)
        }
    }
}

private struct ContentView: View {
    private let metadata = AppMetadata()
    @ObservedObject var repoRootStore: RepoRootStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(metadata.displayName)
                .font(.headline)

            repoRootSummary
        }
        .padding()
        .frame(minWidth: 420, minHeight: 180, alignment: .leading)
        .onAppear {
            repoRootStore.detectRepoRootIfNeeded(promptOnFailure: true)
        }
    }

    @ViewBuilder
    private var repoRootSummary: some View {
        if let repoRootURL = repoRootStore.repoRootURL {
            LabeledContent("Repository Root", value: repoRootURL.path(percentEncoded: false))
        } else if let validationMessage = repoRootStore.repoRootValidationMessage {
            Text(validationMessage)
                .foregroundStyle(.red)
        } else if repoRootStore.isDetectingRepoRoot {
            Text("Detecting repository root...")
                .foregroundStyle(.secondary)
        } else {
            Text("Repository root is not configured.")
                .foregroundStyle(.secondary)
        }
    }
}

private struct SettingsView: View {
    @ObservedObject var repoRootStore: RepoRootStore
    @ObservedObject var onboardingState: OnboardingState

    var body: some View {
        Form {
            LabeledContent("Repository Root") {
                HStack(spacing: 8) {
                    Text(repoRootStore.repoRootDisplayPath)
                        .foregroundStyle(repoRootStore.repoRootURL == nil ? .secondary : .primary)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Button("Change...") {
                        repoRootStore.chooseRepoRoot()
                    }
                    .disabled(repoRootStore.isDetectingRepoRoot || repoRootStore.isChoosingRepoRoot)
                }
            }

            if let validationMessage = repoRootStore.repoRootValidationMessage {
                Text(validationMessage)
                    .foregroundStyle(.red)
            }

            Button("Run Setup Again") {
                onboardingState.restart()
            }
        }
        .padding()
        .frame(minWidth: 520, minHeight: 120)
    }
}

@MainActor
final class RepoRootStore: ObservableObject {
    @Published private(set) var repoRootURL: URL?
    @Published private(set) var isDetectingRepoRoot = false
    @Published private(set) var isChoosingRepoRoot = false
    @Published private(set) var repoRootValidationMessage: String?

    private let defaults: UserDefaults
    private let detectorStartURL: URL
    private var detectionTask: Task<URL?, Never>?

    init(
        defaults: UserDefaults = .standard,
        detectorStartURL: URL = Bundle.main.bundleURL
    ) {
        self.defaults = defaults
        self.detectorStartURL = detectorStartURL
        self.repoRootURL = Self.loadRepoRoot(defaults: defaults)
    }

    var repoRootDisplayPath: String {
        if let repoRootURL {
            return repoRootURL.path(percentEncoded: false)
        }

        return isDetectingRepoRoot ? "Detecting..." : "Not configured"
    }

    func detectRepoRootIfNeeded(promptOnFailure: Bool = false) {
        guard repoRootURL == nil else {
            return
        }

        guard detectionTask == nil else {
            return
        }

        repoRootValidationMessage = nil
        isDetectingRepoRoot = true
        let startURL = detectorStartURL
        let task = Task<URL?, Never> {
            await Task.detached(priority: .userInitiated) {
                RepoDetector.findRepoRoot(startingFrom: startURL)
            }.value
        }
        detectionTask = task

        Task {
            let detectedURL = await task.value

            detectionTask = nil
            isDetectingRepoRoot = false

            guard repoRootURL == nil else {
                return
            }

            if let detectedURL {
                save(detectedURL)
            } else if promptOnFailure {
                repoRootValidationMessage = "Choose the utils repository root."
                chooseRepoRoot()
            }
        }
    }

    func chooseRepoRoot() {
        guard !isChoosingRepoRoot else {
            return
        }

        repoRootValidationMessage = nil

        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.title = "Choose Repository Root"
        panel.prompt = "Choose"
        panel.directoryURL = repoRootURL

        isChoosingRepoRoot = true

        panel.begin { [weak self] response in
            Task { @MainActor [weak self] in
                guard let self else {
                    return
                }

                self.isChoosingRepoRoot = false

                guard response == .OK, let selectedURL = panel.url else {
                    return
                }

                guard RepoDetector.isRepoRoot(selectedURL) else {
                    self.repoRootValidationMessage =
                        "This folder does not look like the utils repository root. " +
                        "Choose a folder containing audio_transcribe_openai.sh or audio_common.py."
                    return
                }

                self.save(selectedURL)
            }
        }
    }

    private func save(_ url: URL) {
        let standardizedURL = url.resolvingSymlinksInPath().standardizedFileURL
        repoRootURL = standardizedURL
        repoRootValidationMessage = nil
        defaults.set(standardizedURL.path(percentEncoded: false), forKey: DefaultsKeys.repoRootPath)
    }

    private static func loadRepoRoot(defaults: UserDefaults) -> URL? {
        guard let savedPath = defaults.string(forKey: DefaultsKeys.repoRootPath),
              !savedPath.isEmpty else {
            return nil
        }

        let savedURL = URL(fileURLWithPath: savedPath, isDirectory: true)
            .resolvingSymlinksInPath()
            .standardizedFileURL

        guard RepoDetector.isRepoRoot(savedURL) else {
            defaults.removeObject(forKey: DefaultsKeys.repoRootPath)
            return nil
        }

        return savedURL
    }
}

enum DefaultsKeys {
    static let repoRootPath = "repoRootPath"
    static let hasCompletedOnboarding = "hasCompletedOnboarding"
}
