import Foundation

public enum OutputPathResolver {
    public static func isVibeVoiceJSON(_ input: URL) -> Bool {
        input.lastPathComponent.lowercased().hasSuffix(".vibevoice.json")
    }

    public static func outputPath(
        for preset: TranscriptionPreset,
        input: URL,
        format: TranscriptFormat? = nil
    ) -> URL {
        let format = format ?? preset.defaultFormat
        precondition(preset.supportedFormats.contains(format), "Unsupported format for preset")

        let prefix: String
        switch preset {
        case .cloud, .privateLocal: prefix = ""
        case .privateLocalWithSpeakers: prefix = format == .speakerText ? "" : "spk."
        case .appleSiliconLocal: prefix = "vibevoice."
        }
        let suffix = format == .speakerText ? "spk.txt" : format.rawValue
        let stem = input.deletingPathExtension().lastPathComponent
        let base = isVibeVoiceJSON(input)
            ? String(stem.dropLast(".vibevoice".count)) : stem
        return input.absoluteURL.deletingLastPathComponent()
            .appendingPathComponent("\(base).\(prefix)\(suffix)", isDirectory: false)
    }

    public static func structuredOutputPath(for input: URL) -> URL {
        outputPath(for: .appleSiliconLocal, input: input).deletingPathExtension()
            .appendingPathExtension("json")
    }
}
