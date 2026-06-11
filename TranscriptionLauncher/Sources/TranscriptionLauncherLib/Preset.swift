/// The transcription presets offered by the launcher, one per backend
/// configuration described in the epic (#65).
public enum Preset: CaseIterable, Equatable, Sendable {
    case fastCloud
    case bestCloud
    case compatibleCloud
    case privateLocal
    case privateLocalWithSpeakers
    case appleSiliconLocal
}
