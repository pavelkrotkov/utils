import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func openAIOutputReplacesExtension() {
    let input = URL(fileURLWithPath: "/tmp/rec.m4a")

    let output = OutputPathResolver.outputPath(for: .cloud, input: input)

    #expect(output.path == "/tmp/rec.txt")
}

@Test
func privateLocalOutputReplacesExtension() {
    let input = URL(fileURLWithPath: "/tmp/rec.m4a")

    let output = OutputPathResolver.outputPath(for: .privateLocal, input: input)

    #expect(output.path == "/tmp/rec.txt")
}

@Test
func diarizedOutputUsesSPK() {
    let input = URL(fileURLWithPath: "/tmp/rec.m4a")

    let output = OutputPathResolver.outputPath(for: .privateLocalWithSpeakers, input: input)

    #expect(output.path == "/tmp/rec.spk.txt")
}

@Test
func vibevoiceOutputUsesVibevoiceSuffix() {
    let input = URL(fileURLWithPath: "/tmp/rec.m4a")

    let output = OutputPathResolver.outputPath(for: .appleSiliconLocal, input: input)

    #expect(output.path == "/tmp/rec.vibevoice.txt")
}

@Test
func fileWithNoExtension() {
    let input = URL(fileURLWithPath: "/tmp/recording")

    let output = OutputPathResolver.outputPath(for: .cloud, input: input)

    #expect(output.path == "/tmp/recording.txt")
}

@Test
func fileWithMultipleDots() {
    let input = URL(fileURLWithPath: "/tmp/my.podcast.ep3.m4a")

    let output = OutputPathResolver.outputPath(for: .cloud, input: input)

    #expect(output.path == "/tmp/my.podcast.ep3.txt")
}

@Test
func fileWithSpacesInName() {
    let input = URL(fileURLWithPath: "/tmp/my recording (1).m4a")

    let output = OutputPathResolver.outputPath(for: .cloud, input: input)

    #expect(output.path == "/tmp/my recording (1).txt")
}

@Test
func allPresetsProduceAbsolutePaths() {
    let input = URL(fileURLWithPath: "/tmp/nested/dir/rec.m4a")

    for preset in TranscriptionPreset.allCases {
        let output = OutputPathResolver.outputPath(for: preset, input: input)

        #expect(output.isFileURL)
        #expect(output.path.hasPrefix("/"))
        #expect(output.deletingLastPathComponent().path == "/tmp/nested/dir")
    }
}

@Test
func relativeInputProducesAbsoluteOutput() {
    let baseURL = URL(fileURLWithPath: "/tmp", isDirectory: true)
    let input = URL(fileURLWithPath: "rec.m4a", relativeTo: baseURL)

    let output = OutputPathResolver.outputPath(for: .cloud, input: input)

    #expect(output.path == "/tmp/rec.txt")
    #expect(output.lastPathComponent == "rec.txt")
}

@Test
func allFormatSuffixesAndCapabilities() {
    let input = URL(fileURLWithPath: "/tmp/recording.m4a")
    let cases: [(TranscriptionPreset, TranscriptFormat, String)] = [
        (.cloud, .markdown, "recording.md"),
        (.privateLocal, .srt, "recording.srt"),
        (.privateLocal, .vtt, "recording.vtt"),
        (.privateLocal, .markdown, "recording.md"),
        (.privateLocalWithSpeakers, .speakerText, "recording.spk.txt"),
        (.privateLocalWithSpeakers, .srt, "recording.spk.srt"),
        (.privateLocalWithSpeakers, .markdown, "recording.spk.md"),
        (.appleSiliconLocal, .plainText, "recording.vibevoice.txt"),
        (.appleSiliconLocal, .speakerText, "recording.vibevoice.spk.txt"),
        (.appleSiliconLocal, .vtt, "recording.vibevoice.vtt"),
        (.appleSiliconLocal, .markdown, "recording.vibevoice.md"),
    ]
    for (preset, format, name) in cases {
        #expect(preset.supportedFormats.contains(format))
        #expect(OutputPathResolver.outputPath(for: preset, input: input, format: format)
            .lastPathComponent == name)
    }
    #expect(TranscriptionPreset.cloud.supportedFormats == [.plainText, .markdown])
    #expect(!TranscriptionPreset.privateLocal.supportedFormats.contains(.speakerText))
}

@Test
func vibevoiceJSONReexportUsesOriginalStem() {
    let input = URL(fileURLWithPath: "/tmp/recording.vibevoice.json")
    #expect(OutputPathResolver.isVibeVoiceJSON(input))
    #expect(OutputPathResolver.outputPath(
        for: .appleSiliconLocal, input: input, format: .markdown
    ).path == "/tmp/recording.vibevoice.md")
    let audio = URL(fileURLWithPath: "/tmp/recording.m4a")
    #expect(OutputPathResolver.structuredOutputPath(for: audio).path == input.path)
}
