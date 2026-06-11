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
    }

    @Published var inputFileURL: URL?
    @Published private(set) var lastOutputURL: URL?
    @Published var errorAlert: ErrorPresentation?
    @Published var pendingOverwriteRun: PendingRun?
    /// True while the environment snapshot is being captured, before
    /// `runner.isRunning` flips; lets the UI show feedback for that phase.
    @Published private(set) var isPreparing = false

    @Published var selectedPreset: TranscriptionPreset {
        didSet { defaults.set(selectedPreset.defaultsValue, forKey: DefaultsKeys.selectedPreset) }
    }
    @Published var whisperModelPath: String {
        didSet { defaults.set(whisperModelPath, forKey: DefaultsKeys.whisperModelPath) }
    }
    @Published var vibevoiceContext: String {
        didSet { defaults.set(vibevoiceContext, forKey: DefaultsKeys.vibevoiceContext) }
    }

    let runner = ProcessRunner()

    private let defaults: UserDefaults
    /// The in-flight run, spanning environment capture and the process run.
    /// Guarding on this instead of `runner.isRunning` closes the window
    /// before `runner.run` starts, where a second Run click would otherwise
    /// launch a duplicate task.
    private var runTask: Task<Void, Never>?

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.selectedPreset = defaults.string(forKey: DefaultsKeys.selectedPreset)
            .flatMap(TranscriptionPreset.init(defaultsValue:)) ?? .fastCloud
        self.whisperModelPath = defaults.string(forKey: DefaultsKeys.whisperModelPath) ?? ""
        self.vibevoiceContext = defaults.string(forKey: DefaultsKeys.vibevoiceContext) ?? ""
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

        let command = CommandBuilder.command(
            for: selectedPreset,
            input: input,
            repoRoot: repoRoot,
            whisperModelPath: selectedPreset.usesWhisperModel
                ? nonEmpty(whisperModelPath) : nil,
            vibevoiceContext: selectedPreset.usesVibeVoiceContext
                ? nonEmpty(vibevoiceContext) : nil
        )
        let output = OutputPathResolver.outputPath(
            for: selectedPreset.outputPathPreset,
            input: input
        )
        let run = PendingRun(command: command, output: output)
        if FileManager.default.fileExists(atPath: output.path(percentEncoded: false)) {
            pendingOverwriteRun = run
        } else {
            start(run)
        }
    }

    func start(_ run: PendingRun) {
        guard runTask == nil else {
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
            let environment = try await EnvironmentSnapshot.capture()
            isPreparing = false
            let outputURL = try await runner.run(command: run.command, environment: environment)
            lastOutputURL = outputURL
            NSWorkspace.shared.activateFileViewerSelecting([outputURL])
        } catch is CancellationError {
            // The user cancelled; the partial log stays visible.
        } catch {
            errorAlert = ErrorPresentation(error: error)
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
