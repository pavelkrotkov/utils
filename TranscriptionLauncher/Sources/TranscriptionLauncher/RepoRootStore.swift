import AppKit
import Foundation
import TranscriptionLauncherLib

/// Persists and validates the utils repository root: loads it from
/// `UserDefaults`, auto-detects it with `RepoDetector`, and lets the user
/// choose it manually with an open panel.
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

        autoDetectRepoRoot(promptOnFailure: promptOnFailure)
    }

    /// Runs `RepoDetector` and saves the result, replacing any previously
    /// configured root.
    func autoDetectRepoRoot(promptOnFailure: Bool = false) {
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

            if let detectedURL {
                save(detectedURL)
            } else if promptOnFailure {
                repoRootValidationMessage = "Choose the utils repository root."
                chooseRepoRoot()
            } else if repoRootURL == nil {
                repoRootValidationMessage = "Could not auto-detect the repository root."
            } else {
                repoRootValidationMessage =
                    "Could not auto-detect the repository root; keeping the current one."
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
