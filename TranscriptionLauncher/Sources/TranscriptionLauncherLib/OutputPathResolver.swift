import Foundation

public enum OutputPathResolver {
    /// Returns the output file URL for a transcription run: the input's
    /// extension is replaced with the preset's suffix, and the file is
    /// placed next to the input.
    public static func outputPath(for preset: Preset, input: URL) -> URL {
        let suffix: String
        switch preset {
        case .fastCloud, .bestCloud, .compatibleCloud, .privateLocal:
            suffix = "txt"
        case .privateLocalWithSpeakers:
            suffix = "spk.txt"
        case .appleSiliconLocal:
            suffix = "vibevoice.txt"
        }

        let stem = input.deletingPathExtension().lastPathComponent
        return input.absoluteURL
            .deletingLastPathComponent()
            .appendingPathComponent("\(stem).\(suffix)", isDirectory: false)
    }
}
