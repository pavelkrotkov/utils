import Foundation

public enum DependencyChecker {
    public struct Item: Equatable, Sendable {
        public let name: String
        public let isAvailable: Bool
        public let resolvedPath: String?
        public let guidance: String

        public init(name: String, isAvailable: Bool, resolvedPath: String? = nil, guidance: String) {
            self.name = name
            self.isAvailable = isAvailable
            self.resolvedPath = resolvedPath
            self.guidance = guidance
        }
    }

    private static let whisperModelName = "ggml-large-v3-turbo-q8_0.bin"
    private static let whisperModelURL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/\(whisperModelName)"

    public static func check(
        preset: TranscriptionPreset,
        environment: [String: String],
        repoRoot: URL?,
        whisperModelPath: String? = nil,
        inputFile: URL? = nil,
        osMajorVersion: Int? = nil,
        isAppleSilicon: Bool? = nil
    ) -> [Item] {
        let script: String
        switch preset {
        case .fastCloud, .bestCloud, .compatibleCloud: script = "audio_transcribe_openai.sh"
        case .privateLocal, .privateLocalWithSpeakers: script = "audio_transcribe_whisper.py"
        case .appleSiliconLocal: script = "audio_transcribe_vibevoice.py"
        }
        let scriptURL = repoRoot?.appendingPathComponent(script)
        let scriptPath = scriptURL?.path(percentEncoded: false)
        var items = [Item(
            name: script,
            isAvailable: scriptPath.map {
                readableFile($0)
                    && (script.hasSuffix(".py") || FileManager.default.isExecutableFile(atPath: $0))
            } ?? false,
            resolvedPath: scriptPath,
            guidance: "Clone https://github.com/pavelkrotkov/utils.git in Terminal, then choose that folder as Repository Root in Settings."
        )]
        items.append(Item(
            name: "macOS 14+",
            isAvailable: (osMajorVersion ?? ProcessInfo.processInfo.operatingSystemVersion.majorVersion) >= 14,
            guidance: "macOS 14 or newer is required."
        ))

        switch preset {
        case .fastCloud, .bestCloud, .compatibleCloud:
            items += [executable("curl", environment: environment), executable("jq", environment: environment)]
            if let inputFile,
               ((try? inputFile.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) > 25 * 1024 * 1024 {
                items.append(executable("ffmpeg", environment: environment))
            }
            items.append(variable("OPENAI_API_KEY", environment: environment))
        case .privateLocal, .privateLocalWithSpeakers:
            items += [executable("uv", environment: environment), executable("ffmpeg", environment: environment)]
            let whisper = ["whisper-cpp", "whisper-cli"]
                .compactMap { ExecutableResolver.resolve($0, environment: environment) }.first
            items.append(Item(
                name: "whisper-cpp / whisper-cli",
                isAvailable: whisper != nil,
                resolvedPath: whisper?.path(percentEncoded: false),
                guidance: "In Terminal: brew install whisper.cpp (Homebrew provides whisper-cli)."
            ))
            items.append(whisperModel(whisperModelPath))
            if preset == .privateLocalWithSpeakers {
                items.append(variable("HF_TOKEN", environment: environment))
            }
        case .appleSiliconLocal:
            items += [executable("uv", environment: environment), executable("ffmpeg", environment: environment)]
            #if arch(arm64)
            let supportedHardware = isAppleSilicon ?? true
            #else
            let supportedHardware = isAppleSilicon ?? false
            #endif
            items.append(Item(
                name: "Apple Silicon",
                isAvailable: supportedHardware,
                guidance: "VibeVoice requires an Apple Silicon Mac running native arm64 code."
            ))
            items.append(vibeVoiceModel(environment: environment))
        }
        return items
    }

    private static func executable(_ name: String, environment: [String: String]) -> Item {
        let url = ExecutableResolver.resolve(name, environment: environment)
        return Item(
            name: name,
            isAvailable: url != nil,
            resolvedPath: url?.path(percentEncoded: false),
            guidance: "In Terminal: brew install \(name). If Finder cannot find Homebrew tools, add eval \"$(/opt/homebrew/bin/brew shellenv)\" to ~/.zprofile."
        )
    }

    private static func variable(_ name: String, environment: [String: String]) -> Item {
        let value = environment[name]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let guidance = name == "HF_TOKEN"
            ? "Only needed for speaker diarization. Set HF_TOKEN in your shell profile and accept the pyannote model terms on Hugging Face."
            : "Only needed for cloud transcription. Set OPENAI_API_KEY in your shell profile, then recheck."
        return Item(name: name, isAvailable: !value.isEmpty, guidance: guidance)
    }

    private static func whisperModel(_ customPath: String?) -> Item {
        let custom = customPath?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let path = custom.isEmpty
            ? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("models/\(whisperModelName)").path
            : custom
        let download = "In Terminal (~874 MB, with progress): mkdir -p \"$HOME/models\" && curl -fL --progress-bar \"\(whisperModelURL)\" -o \"$HOME/models/\(whisperModelName).part\" && mv \"$HOME/models/\(whisperModelName).part\" \"$HOME/models/\(whisperModelName)\""
        return Item(
            name: "Whisper model",
            isAvailable: path.hasPrefix("/") && readableFile(path),
            resolvedPath: path,
            guidance: custom.isEmpty ? download : "Select an existing, nonempty model .bin file using Settings → Whisper Model (absolute path)."
        )
    }

    private static func vibeVoiceModel(environment: [String: String]) -> Item {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let hub = environment["HF_HUB_CACHE"]
            ?? "\(environment["HF_HOME"] ?? "\(home)/.cache/huggingface")/hub"
        let snapshots = "\(hub)/models--mlx-community--VibeVoice-ASR-4bit/snapshots"
        let versions = (try? FileManager.default.contentsOfDirectory(atPath: snapshots)) ?? []
        let files = ["config.json", "model.safetensors.index.json", "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"]
        let available = versions.contains { version in
            files.allSatisfy { readableFile("\(snapshots)/\(version)/\($0)") }
        }
        return Item(
            name: "VibeVoice model (5.7 GB)",
            isAvailable: available,
            guidance: "In Terminal, explicitly download with progress: uvx --from huggingface_hub hf download mlx-community/VibeVoice-ASR-4bit. No token is needed for this public model."
        )
    }

    private static func readableFile(_ path: String) -> Bool {
        let manager = FileManager.default
        let resolved = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
        guard let attributes = try? manager.attributesOfItem(atPath: resolved),
              attributes[.type] as? FileAttributeType == .typeRegular,
              let size = attributes[.size] as? NSNumber else {
            return false
        }
        return size.intValue > 0 && manager.isReadableFile(atPath: resolved)
    }
}
