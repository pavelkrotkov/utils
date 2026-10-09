import Foundation

public enum TranscriptionPreset: String, CaseIterable, Equatable, Sendable {
    case cloud
    case privateLocal
    case privateLocalWithSpeakers
    case appleSiliconLocal

    public var isCloud: Bool {
        switch self {
        case .cloud: true
        case .privateLocal, .privateLocalWithSpeakers, .appleSiliconLocal: false
        }
    }
}

public struct TranscriptionCommand: Equatable, Sendable {
    /// Absolute path, or a bare command name (e.g. `uv`) that the process
    /// runner must resolve against the captured login-shell PATH.
    public let executable: String
    public let arguments: [String]
    public let workingDirectory: URL
    /// File the command writes the transcript to; the process runner
    /// verifies it exists after a successful exit.
    public let outputFile: URL

    public init(
        executable: String,
        arguments: [String],
        workingDirectory: URL,
        outputFile: URL
    ) {
        self.executable = executable
        self.arguments = arguments
        self.workingDirectory = workingDirectory
        self.outputFile = outputFile
    }
}

public enum CommandBuilder {
    public static func command(
        for preset: TranscriptionPreset,
        input: URL,
        repoRoot: URL,
        whisperModelPath: String? = nil,
        vibevoiceContext: String? = nil,
        vibevoiceChunkSeconds: Int = 0
    ) -> TranscriptionCommand {
        precondition(input.isFileURL, "Input URL must be a file URL")
        precondition(repoRoot.isFileURL, "Repository root URL must be a file URL")

        let inputPath = input.path
        let outputPath = Self.outputPath(for: preset, input: input)

        switch preset {
        case .cloud:
            return openAICommand(
                model: "gpt-transcribe",
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
            var options = ["--format", "txt", "--chunk-seconds", String(vibevoiceChunkSeconds)]
            if let vibevoiceContext {
                options += ["--context", vibevoiceContext]
            }
            return uvCommand(
                scriptName: "audio_transcribe_vibevoice.py",
                options: options,
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot
            )
        }
    }

    /// Must stay in sync with the OutputPathResolver naming rules (#58):
    /// `.txt` for plain transcripts, `.spk.txt` for diarized output,
    /// `.vibevoice.txt` for VibeVoice output.
    private static func outputPath(for preset: TranscriptionPreset, input: URL) -> String {
        let suffix: String
        switch preset {
        case .privateLocalWithSpeakers:
            suffix = ".spk.txt"
        case .appleSiliconLocal:
            suffix = ".vibevoice.txt"
        case .cloud, .privateLocal:
            suffix = ".txt"
        }
        return input.deletingPathExtension().path + suffix
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
            workingDirectory: repoRoot,
            outputFile: URL(fileURLWithPath: outputPath, isDirectory: false)
        )
    }

    private static func whisperCommand(
        options: [String],
        inputPath: String,
        outputPath: String,
        repoRoot: URL,
        whisperModelPath: String?
    ) -> TranscriptionCommand {
        var options = options
        if let whisperModelPath {
            options += ["--large-model", whisperModelPath]
        }
        return uvCommand(
            scriptName: "audio_transcribe_whisper.py",
            options: options,
            inputPath: inputPath,
            outputPath: outputPath,
            repoRoot: repoRoot
        )
    }

    private static func uvCommand(
        scriptName: String,
        options: [String],
        inputPath: String,
        outputPath: String,
        repoRoot: URL
    ) -> TranscriptionCommand {
        var arguments = [
            "run", repoRoot.appendingPathComponent(scriptName).path,
            inputPath,
        ]
        arguments += options
        arguments += ["-o", outputPath]
        return TranscriptionCommand(
            executable: "uv",
            arguments: arguments,
            workingDirectory: repoRoot,
            outputFile: URL(fileURLWithPath: outputPath, isDirectory: false)
        )
    }
}
