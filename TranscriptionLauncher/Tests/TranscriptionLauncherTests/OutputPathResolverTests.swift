import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func openAIOutputReplacesExtension() {
    let input = URL(fileURLWithPath: "/tmp/rec.m4a")

    for preset: Preset in [.fastCloud, .bestCloud, .compatibleCloud] {
        let output = OutputPathResolver.outputPath(for: preset, input: input)

        #expect(output.path == "/tmp/rec.txt")
    }
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

    let output = OutputPathResolver.outputPath(for: .fastCloud, input: input)

    #expect(output.path == "/tmp/recording.txt")
}

@Test
func fileWithMultipleDots() {
    let input = URL(fileURLWithPath: "/tmp/my.podcast.ep3.m4a")

    let output = OutputPathResolver.outputPath(for: .fastCloud, input: input)

    #expect(output.path == "/tmp/my.podcast.ep3.txt")
}

@Test
func fileWithSpacesInName() {
    let input = URL(fileURLWithPath: "/tmp/my recording (1).m4a")

    let output = OutputPathResolver.outputPath(for: .fastCloud, input: input)

    #expect(output.path == "/tmp/my recording (1).txt")
}

@Test
func allPresetsProduceAbsolutePaths() {
    let input = URL(fileURLWithPath: "/tmp/nested/dir/rec.m4a")

    for preset in Preset.allCases {
        let output = OutputPathResolver.outputPath(for: preset, input: input)

        #expect(output.isFileURL)
        #expect(output.path.hasPrefix("/"))
        #expect(output.deletingLastPathComponent().path == "/tmp/nested/dir")
    }
}

@Test
func relativeInputProducesAbsoluteOutput() {
    let input = URL(fileURLWithPath: "rec.m4a")

    let output = OutputPathResolver.outputPath(for: .fastCloud, input: input)

    #expect(output.path.hasPrefix("/"))
    #expect(output.lastPathComponent == "rec.txt")
}
