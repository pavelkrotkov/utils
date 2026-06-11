import Foundation

public enum TranscriptionPreset: CaseIterable, Equatable, Sendable {
    case fastCloud
    case bestCloud
    case compatibleCloud
    case privateLocal
    case privateLocalWithSpeakers
    case appleSiliconLocal
}

public struct TranscriptionCommand: Equatable, Sendable {
    /// Absolute path, or a bare command name (e.g. `uv`) that the process
    /// runner must resolve against the captured login-shell PATH.
    public let executable: String
    public let arguments: [String]
    public let workingDirectory: URL

    public init(executable: String, arguments: [String], workingDirectory: URL) {
        self.executable = executable
        self.arguments = arguments
        self.workingDirectory = workingDirectory
    }
}

public enum CommandBuilder {
    public static func command(
        for preset: TranscriptionPreset,
        input: URL,
        repoRoot: URL,
        whisperModelPath: String? = nil,
        vibevoiceContext: String? = nil
    ) -> TranscriptionCommand {
        precondition(input.isFileURL, "Input URL must be a file URL")
        precondition(repoRoot.isFileURL, "Repository root URL must be a file URL")

        let inputPath = input.path
        let outputPath = input.deletingPathExtension().appendingPathExtension("txt").path

        switch preset {
        case .fastCloud:
            return openAICommand(
                model: "gpt-4o-mini-transcribe",
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot
            )
        case .bestCloud:
            return openAICommand(
                model: "gpt-4o-transcribe",
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot
            )
        case .compatibleCloud:
            return openAICommand(
                model: "whisper-1",
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot
            )
        case .privateLocal:
            return whisperCommand(
                options: ["--format", "txt"],
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot,
                whisperModelPath: whisperModelPath
            )
        case .privateLocalWithSpeakers:
            return whisperCommand(
                options: ["--diarization"],
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot,
                whisperModelPath: whisperModelPath
            )
        case .appleSiliconLocal:
            var arguments = [
                "run", repoRoot.appendingPathComponent("audio_transcribe_vibevoice.py").path,
                inputPath,
                "--format", "txt",
                "-o", outputPath,
            ]
            if let vibevoiceContext {
                arguments += ["--context", vibevoiceContext]
            }
            return TranscriptionCommand(
                executable: "uv",
                arguments: arguments,
                workingDirectory: repoRoot
            )
        }
    }

    private static func openAICommand(
        model: String,
        inputPath: String,
        outputPath: String,
        repoRoot: URL
    ) -> TranscriptionCommand {
        TranscriptionCommand(
            executable: repoRoot.appendingPathComponent("audio_transcribe_openai.sh").path,
            arguments: ["--model", model, inputPath, outputPath],
            workingDirectory: repoRoot
        )
    }

    private static func whisperCommand(
        options: [String],
        inputPath: String,
        outputPath: String,
        repoRoot: URL,
        whisperModelPath: String?
    ) -> TranscriptionCommand {
        var arguments = [
            "run", repoRoot.appendingPathComponent("audio_transcribe_whisper.py").path,
            inputPath,
        ]
        arguments += options
        arguments += ["-o", outputPath]
        if let whisperModelPath {
            arguments += ["--large-model", whisperModelPath]
        }
        return TranscriptionCommand(
            executable: "uv",
            arguments: arguments,
            workingDirectory: repoRoot
        )
    }
}
