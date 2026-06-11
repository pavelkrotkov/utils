import TranscriptionLauncherLib

extension TranscriptionPreset {
    /// Picker groups, in the order the epic lists them.
    static let cloudPresets: [TranscriptionPreset] = [.fastCloud, .bestCloud, .compatibleCloud]
    static let localPresets: [TranscriptionPreset] = [
        .privateLocal, .privateLocalWithSpeakers, .appleSiliconLocal,
    ]

    var displayName: String {
        switch self {
        case .fastCloud: "Fast cloud"
        case .bestCloud: "Best cloud"
        case .compatibleCloud: "Compatible cloud"
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
        case .fastCloud: .fastCloud
        case .bestCloud: .bestCloud
        case .compatibleCloud: .compatibleCloud
        case .privateLocal: .privateLocal
        case .privateLocalWithSpeakers: .privateLocalWithSpeakers
        case .appleSiliconLocal: .appleSiliconLocal
        }
    }

    /// Stable identifier used to persist the selection in `UserDefaults`.
    var defaultsValue: String {
        switch self {
        case .fastCloud: "fastCloud"
        case .bestCloud: "bestCloud"
        case .compatibleCloud: "compatibleCloud"
        case .privateLocal: "privateLocal"
        case .privateLocalWithSpeakers: "privateLocalWithSpeakers"
        case .appleSiliconLocal: "appleSiliconLocal"
        }
    }

    init?(defaultsValue: String) {
        guard let preset = Self.allCases.first(where: { $0.defaultsValue == defaultsValue }) else {
            return nil
        }
        self = preset
    }
}
