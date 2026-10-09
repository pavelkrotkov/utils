import SwiftUI
import TranscriptionLauncherLib

extension TranscriptionPreset {
    static let cloudPresets: [TranscriptionPreset] = [.cloud]
    static let localPresets: [TranscriptionPreset] = [
        .privateLocal, .privateLocalWithSpeakers, .appleSiliconLocal,
    ]

    var displayName: String {
        switch self {
        case .cloud: "OpenAI cloud"
        case .privateLocal: "Private local"
        case .privateLocalWithSpeakers: "Private local with speakers"
        case .appleSiliconLocal: "Apple Silicon local"
        }
    }

    /// Whisper presets accept the optional model path from Settings.
    var usesWhisperModel: Bool {
        self == .privateLocal || self == .privateLocalWithSpeakers
    }

    /// The VibeVoice preset accepts optional hotword/domain context from
    /// Settings.
    var usesVibeVoiceContext: Bool {
        self == .appleSiliconLocal
    }

    /// The `OutputPathResolver` counterpart of this preset.
    var outputPathPreset: Preset {
        switch self {
        case .cloud: .cloud
        case .privateLocal: .privateLocal
        case .privateLocalWithSpeakers: .privateLocalWithSpeakers
        case .appleSiliconLocal: .appleSiliconLocal
        }
    }

    var privacyDescription: String {
        isCloud
            ? "Cloud uploads audio to OpenAI. Your own API key is required and usage may cost money."
            : "On-device transcription. Your audio stays on this Mac; no cloud API key is needed."
    }

}

struct ReadinessChecklist: View {
    let items: [DependencyChecker.Item]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(items, id: \.name) { item in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: item.isAvailable ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(item.isAvailable ? .green : .orange)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(item.name).font(.system(.subheadline, design: .monospaced))
                        if item.isAvailable {
                            Text(item.resolvedPath.map { "Found: \($0)" } ?? "Ready")
                                .foregroundStyle(.secondary)
                        } else {
                            Text(item.guidance)
                                .textSelection(.enabled)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .font(.caption)
                }
            }
        }
    }
}
