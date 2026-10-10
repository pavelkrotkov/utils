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

public enum TranscriptFormat: String, CaseIterable, Equatable, Sendable {
    case plainText = "txt"
    case srt
    case vtt
    case speakerText = "diarized-txt"
    case markdown = "md"

    public var displayName: String {
        switch self {
        case .plainText: "Plain text"
        case .srt: "Subtitles (SRT)"
        case .vtt: "Subtitles (VTT)"
        case .speakerText: "Speaker-labeled text"
        case .markdown: "Markdown (Obsidian)"
        }
    }
}

extension TranscriptionPreset {
    public var supportedFormats: [TranscriptFormat] {
        switch self {
        case .cloud: [.plainText, .markdown]
        case .privateLocal: [.plainText, .srt, .vtt, .markdown]
        case .privateLocalWithSpeakers: [.speakerText, .srt, .vtt, .markdown]
        case .appleSiliconLocal: [.plainText, .speakerText, .srt, .vtt, .markdown]
        }
    }

    public var defaultFormat: TranscriptFormat {
        self == .privateLocalWithSpeakers ? .speakerText : .plainText
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
    public let additionalOutputFiles: [URL]

    public var outputFiles: [URL] { [outputFile] + additionalOutputFiles }

    public init(
        executable: String,
        arguments: [String],
        workingDirectory: URL,
        outputFile: URL,
        additionalOutputFiles: [URL] = []
    ) {
        self.executable = executable
        self.arguments = arguments
        self.workingDirectory = workingDirectory
        self.outputFile = outputFile
        self.additionalOutputFiles = additionalOutputFiles
    }
}

public enum CommandBuilder {
    public static func command(
        for preset: TranscriptionPreset,
        input: URL,
        repoRoot: URL,
        whisperModelPath: String? = nil,
        vibevoiceContext: String? = nil,
        vibevoiceChunkSeconds: Int = 0,
        format: TranscriptFormat? = nil,
        keepVibeVoiceJSON: Bool = false
    ) -> TranscriptionCommand {
        precondition(input.isFileURL, "Input URL must be a file URL")
        precondition(repoRoot.isFileURL, "Repository root URL must be a file URL")

        let chosenFormat = format ?? preset.defaultFormat
        let output = OutputPathResolver.outputPath(for: preset, input: input, format: chosenFormat)
        let inputPath = input.path
        let outputPath = output.path

        if OutputPathResolver.isVibeVoiceJSON(input) {
            precondition(preset == .appleSiliconLocal, "JSON re-export requires VibeVoice")
            return TranscriptionCommand(
                executable: "python3",
                arguments: [
                    repoRoot.appendingPathComponent("audio_transcribe_vibevoice.py").path,
                    "--from-json", inputPath, "--format", chosenFormat.rawValue,
                    "-o", outputPath,
                ],
                workingDirectory: repoRoot,
                outputFile: output
            )
        }

        switch preset {
        case .cloud:
            return openAICommand(
                model: "gpt-transcribe",
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot,
                format: chosenFormat
            )
        case .privateLocal:
            return whisperCommand(
                options: ["--format", chosenFormat.rawValue],
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot,
                whisperModelPath: whisperModelPath
            )
        case .privateLocalWithSpeakers:
            return whisperCommand(
                options: ["--diarization", "--format", chosenFormat.rawValue],
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot,
                whisperModelPath: whisperModelPath
            )
        case .appleSiliconLocal:
            var options = ["--format", chosenFormat.rawValue, "--chunk-seconds", String(vibevoiceChunkSeconds)]
            if let vibevoiceContext {
                options += ["--context", vibevoiceContext]
            }
            if keepVibeVoiceJSON {
                options.append("--keep-json")
            }
            return uvCommand(
                scriptName: "audio_transcribe_vibevoice.py",
                options: options,
                inputPath: inputPath,
                outputPath: outputPath,
                repoRoot: repoRoot,
                additionalOutputFiles: keepVibeVoiceJSON
                    ? [OutputPathResolver.structuredOutputPath(for: input)] : []
            )
        }
    }

    private static func openAICommand(
        model: String,
        inputPath: String,
        outputPath: String,
        repoRoot: URL,
        format: TranscriptFormat
    ) -> TranscriptionCommand {
        TranscriptionCommand(
            executable: repoRoot.appendingPathComponent("audio_transcribe_openai.sh").path,
            arguments: ["--model", model]
                + (format == .markdown ? ["--format", "md"] : [])
                + [inputPath, outputPath],
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
        repoRoot: URL,
        additionalOutputFiles: [URL] = []
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
            outputFile: URL(fileURLWithPath: outputPath, isDirectory: false),
            additionalOutputFiles: additionalOutputFiles
        )
    }
}
