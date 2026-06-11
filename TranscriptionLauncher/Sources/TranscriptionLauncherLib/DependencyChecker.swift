import Foundation

/// Checks whether the external tools and credentials the transcription
/// presets rely on are available in a captured environment.
public enum DependencyChecker {
    public enum Requirement: Equatable, Sendable {
        case localPresets
        case cloudPresets
        case speakerDiarization
    }

    public struct Item: Equatable, Sendable {
        public let name: String
        public let requirement: Requirement
        public let isAvailable: Bool
        /// Absolute path of the resolved executable; nil for environment
        /// variables and for executables that were not found.
        public let resolvedPath: String?

        public init(
            name: String,
            requirement: Requirement,
            isAvailable: Bool,
            resolvedPath: String? = nil
        ) {
            self.name = name
            self.requirement = requirement
            self.isAvailable = isAvailable
            self.resolvedPath = resolvedPath
        }
    }

    /// Executables every local preset needs on `PATH`.
    public static let localExecutableNames = ["ffmpeg", "uv"]

    /// The whisper binary the whisper local presets need, in lookup order:
    /// current Homebrew formulae install `whisper-cli`, which
    /// audio_transcribe_whisper.py accepts as a fallback for `whisper-cpp`.
    public static let whisperExecutableNames = ["whisper-cpp", "whisper-cli"]

    /// Environment variables the cloud presets need.
    public static let cloudVariableNames = ["OPENAI_API_KEY"]

    /// Environment variables speaker diarization needs: pyannote models are
    /// fetched from Hugging Face during the diarization preset.
    public static let diarizationVariableNames = ["HF_TOKEN"]

    /// Availability is advisory: a missing entry means the matching presets
    /// won't run, not that the app is unusable.
    public static func check(environment: [String: String]) -> [Item] {
        var items: [Item] = []

        for name in localExecutableNames {
            let resolvedURL = ExecutableResolver.resolve(name, environment: environment)
            items.append(Item(
                name: name,
                requirement: .localPresets,
                isAvailable: resolvedURL != nil,
                resolvedPath: resolvedURL?.path(percentEncoded: false)
            ))
        }

        var whisperURL: URL?
        for name in whisperExecutableNames {
            if let resolvedURL = ExecutableResolver.resolve(name, environment: environment) {
                whisperURL = resolvedURL
                break
            }
        }
        items.append(Item(
            name: "whisper-cpp",
            requirement: .localPresets,
            isAvailable: whisperURL != nil,
            resolvedPath: whisperURL?.path(percentEncoded: false)
        ))

        for name in cloudVariableNames {
            items.append(variableItem(name, requirement: .cloudPresets, environment: environment))
        }

        for name in diarizationVariableNames {
            items.append(variableItem(name, requirement: .speakerDiarization, environment: environment))
        }

        return items
    }

    private static func variableItem(
        _ name: String,
        requirement: Requirement,
        environment: [String: String]
    ) -> Item {
        let value = environment[name]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return Item(
            name: name,
            requirement: requirement,
            isAvailable: !value.isEmpty
        )
    }
}
