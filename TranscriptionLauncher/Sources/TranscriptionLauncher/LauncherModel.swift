import AppKit
import Foundation
import TranscriptionLauncherLib
import UniformTypeIdentifiers

/// Drives a transcription run: holds the dropped input file, the selected
/// preset, and the Settings-backed options, and wires them into
/// `CommandBuilder`, `OutputPathResolver`, and `ProcessRunner`.
@MainActor
final class LauncherModel: ObservableObject {
    /// A validated run with its command fully built up front, so preset or
    /// option changes made while the overwrite confirmation is showing (or
    /// while the environment snapshot is being captured) cannot alter what
    /// actually executes.
    struct PendingRun: Equatable {
        let command: TranscriptionCommand
        let output: URL
        let input: URL
        let preset: TranscriptionPreset
        let whisperModelPath: String?
    }

    @Published var inputFileURL: URL?
    @Published private(set) var lastOutputURL: URL?
    @Published var errorAlert: ErrorPresentation?
    @Published var pendingOverwriteRun: PendingRun?
    @Published var pendingDownloadRun: PendingRun?
    @Published private(set) var pendingCloudPreset: TranscriptionPreset?
    /// True while the environment snapshot is being captured, before
    /// `runner.isRunning` flips; lets the UI show feedback for that phase.
    @Published private(set) var isPreparing = false

    @Published private(set) var selectedPreset: TranscriptionPreset {
        didSet { preferences.selectedPreset = selectedPreset }
    }
    @Published var whisperModelPath: String {
        didSet { defaults.set(whisperModelPath, forKey: DefaultsKeys.whisperModelPath) }
    }
    @Published var vibevoiceContext: String {
        didSet { defaults.set(vibevoiceContext, forKey: DefaultsKeys.vibevoiceContext) }
    }

    let runner = ProcessRunner()

    private let notifications = NotificationManager()
    private let defaults: UserDefaults
    private let preferences: PresetPreferences
    /// The in-flight run, spanning environment capture and the process run.
    /// Guarding on this instead of `runner.isRunning` closes the window
    /// before `runner.run` starts, where a second Run click would otherwise
    /// launch a duplicate task.
    private var runTask: Task<Void, Never>?

    init(defaults: UserDefaults = .standard) {
        let preferences = PresetPreferences(defaults: defaults)
        self.defaults = defaults
        self.preferences = preferences
        self.selectedPreset = preferences.selectedPreset
        self.whisperModelPath = defaults.string(forKey: DefaultsKeys.whisperModelPath) ?? ""
        self.vibevoiceContext = defaults.string(forKey: DefaultsKeys.vibevoiceContext) ?? ""
    }

    func selectPreset(_ preset: TranscriptionPreset) {
        if preferences.requiresConsent(for: preset) {
            pendingCloudPreset = preset
        } else {
            selectedPreset = preset
        }
    }

    func approveCloudSelection(_ preset: TranscriptionPreset) {
        guard preset.isCloud else { return }
        preferences.cloudUploadApproved = true
        selectedPreset = preset
        pendingCloudPreset = nil
    }

    func dismissCloudSelection() {
        pendingCloudPreset = nil
    }

    /// Accepts the first dropped or Finder-opened file when it is an
    /// existing audio or video file; otherwise explains why it was rejected.
    @discardableResult
    func acceptInputFiles(_ urls: [URL]) -> Bool {
        guard let url = urls.first else {
            return false
        }

        guard FileManager.default.fileExists(atPath: url.path(percentEncoded: false)) else {
            errorAlert = ErrorPresentation(
                title: "File Not Found",
                message: "\(url.lastPathComponent) does not exist."
            )
            return false
        }

        guard Self.isAudioOrVideoFile(url) else {
            errorAlert = ErrorPresentation(
                title: "Unsupported File Type",
                message: "\(url.lastPathComponent) is not an audio or video file."
            )
            return false
        }

        inputFileURL = url
        lastOutputURL = nil
        return true
    }

    /// Validates the run and starts it, first setting `pendingOverwriteRun`
    /// for confirmation when the output file already exists.
    func requestRun(repoRoot: URL?) {
        guard runTask == nil, let input = inputFileURL else {
            return
        }

        guard let repoRoot else {
            errorAlert = ErrorPresentation(
                title: "Repository Root Not Configured",
                message: "Choose the utils repository root in Settings before running."
            )
            return
        }

        guard FileManager.default.fileExists(atPath: input.path(percentEncoded: false)) else {
            errorAlert = ErrorPresentation(
                title: "File Not Found",
                message: "\(input.lastPathComponent) no longer exists. Drop the file again."
            )
            inputFileURL = nil
            return
        }

        guard !preferences.requiresConsent(for: selectedPreset) else {
            pendingCloudPreset = selectedPreset
            return
        }

        let modelPath = selectedPreset.usesWhisperModel ? nonEmpty(whisperModelPath) : nil
        let command = CommandBuilder.command(
            for: selectedPreset,
            input: input,
            repoRoot: repoRoot,
            whisperModelPath: modelPath,
            vibevoiceContext: selectedPreset.usesVibeVoiceContext
                ? nonEmpty(vibevoiceContext) : nil
        )
        let output = OutputPathResolver.outputPath(
            for: selectedPreset.outputPathPreset,
            input: input
        )
        let run = PendingRun(
            command: command,
            output: output,
            input: input,
            preset: selectedPreset,
            whisperModelPath: modelPath
        )
        if selectedPreset.isCloud {
            confirmOverwrite(run)
        } else {
            pendingDownloadRun = run
        }
    }

    func approveLocalRun(_ run: PendingRun) {
        pendingDownloadRun = nil
        confirmOverwrite(run)
    }

    private func confirmOverwrite(_ run: PendingRun) {
        if FileManager.default.fileExists(atPath: run.output.path(percentEncoded: false)) {
            pendingOverwriteRun = run
        } else {
            start(run)
        }
    }

    func start(_ run: PendingRun) {
        guard runTask == nil else {
            return
        }
        guard !preferences.requiresConsent(for: run.preset) else {
            pendingCloudPreset = run.preset
            return
        }

        lastOutputURL = nil
        isPreparing = true
        runTask = Task {
            await perform(run)
            runTask = nil
        }
    }

    func cancel() {
        runner.cancel()
    }

    func revealLastOutputInFinder() {
        guard let lastOutputURL else {
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([lastOutputURL])
    }

    private func perform(_ run: PendingRun) async {
        defer { isPreparing = false }
        do {
            let environment = try await EnvironmentSnapshot.refresh()
            let missing = DependencyChecker.check(
                preset: run.preset,
                environment: environment,
                repoRoot: run.command.workingDirectory,
                whisperModelPath: run.whisperModelPath,
                inputFile: run.input
            ).filter { !$0.isAvailable }
            guard missing.isEmpty else {
                errorAlert = ErrorPresentation(
                    title: "Setup Required",
                    message: missing.map { "\($0.name): \($0.guidance)" }.joined(separator: "\n\n")
                )
                return
            }
            isPreparing = false
            let outputURL = try await runner.run(command: run.command, environment: environment)
            lastOutputURL = outputURL
            if NSApp.isActive {
                NSWorkspace.shared.activateFileViewerSelecting([outputURL])
            } else {
                // The user is in another app: don't steal focus by opening
                // Finder; notify instead, and reveal on click.
                notifications.notifySuccess(output: outputURL)
            }
        } catch is CancellationError {
            // The user cancelled; the partial log stays visible.
        } catch {
            let presentation = ErrorPresentation(error: error)
            errorAlert = presentation
            if !NSApp.isActive {
                notifications.notifyFailure(message: presentation.message)
            }
        }
    }

    private func nonEmpty(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func isAudioOrVideoFile(_ url: URL) -> Bool {
        guard let type = try? url.resourceValues(forKeys: [.contentTypeKey]).contentType else {
            return false
        }
        return type.conforms(to: .audio) || type.conforms(to: .movie)
    }
}
