import Foundation
import Testing
import TranscriptionLauncherLib

private let repoRoot = URL(fileURLWithPath: "/Users/me/utils", isDirectory: true)
private let input = URL(fileURLWithPath: "/Users/me/Recordings/meeting.m4a")

@Test
func testCloudPreset() {
    let command = CommandBuilder.command(for: .cloud, input: input, repoRoot: repoRoot)

    #expect(command.executable == "/Users/me/utils/audio_transcribe_openai.sh")
    #expect(command.arguments == [
        "--model", "gpt-transcribe",
        "/Users/me/Recordings/meeting.m4a",
        "/Users/me/Recordings/meeting.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testPrivateLocalPreset() {
    let command = CommandBuilder.command(for: .privateLocal, input: input, repoRoot: repoRoot)

    #expect(command.executable == "uv")
    #expect(command.arguments == [
        "run", "/Users/me/utils/audio_transcribe_whisper.py",
        "/Users/me/Recordings/meeting.m4a",
        "--format", "txt",
        "-o", "/Users/me/Recordings/meeting.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testPrivateLocalWithSpeakersPreset() {
    let command = CommandBuilder.command(
        for: .privateLocalWithSpeakers,
        input: input,
        repoRoot: repoRoot
    )

    #expect(command.executable == "uv")
    #expect(command.arguments == [
        "run", "/Users/me/utils/audio_transcribe_whisper.py",
        "/Users/me/Recordings/meeting.m4a",
        "--diarization", "--format", "diarized-txt",
        "-o", "/Users/me/Recordings/meeting.spk.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testAppleSiliconLocalPreset() {
    let command = CommandBuilder.command(for: .appleSiliconLocal, input: input, repoRoot: repoRoot)

    #expect(command.executable == "uv")
    #expect(command.arguments == [
        "run", "/Users/me/utils/audio_transcribe_vibevoice.py",
        "/Users/me/Recordings/meeting.m4a",
        "--format", "txt", "--chunk-seconds", "0",
        "-o", "/Users/me/Recordings/meeting.vibevoice.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testSpacesInFilename() {
    // Arguments are an array, not a shell string — spaces are safe
    let spacedInput = URL(fileURLWithPath: "/Users/me/My Recordings/team sync.m4a")

    let command = CommandBuilder.command(for: .cloud, input: spacedInput, repoRoot: repoRoot)

    #expect(command.arguments == [
        "--model", "gpt-transcribe",
        "/Users/me/My Recordings/team sync.m4a",
        "/Users/me/My Recordings/team sync.txt",
    ])
}

@Test
func testWhisperPresetUsesUv() {
    let plain = CommandBuilder.command(for: .privateLocal, input: input, repoRoot: repoRoot)
    let speakers = CommandBuilder.command(
        for: .privateLocalWithSpeakers,
        input: input,
        repoRoot: repoRoot
    )

    #expect(plain.executable == "uv")
    #expect(speakers.executable == "uv")
}

@Test
func testVibevoiceOptionsInjected() {
    let command = CommandBuilder.command(
        for: .appleSiliconLocal,
        input: input,
        repoRoot: repoRoot,
        vibevoiceContext: "Team standup about the launcher",
        vibevoiceChunkSeconds: 120
    )

    #expect(command.arguments == [
        "run", "/Users/me/utils/audio_transcribe_vibevoice.py",
        "/Users/me/Recordings/meeting.m4a",
        "--format", "txt",
        "--chunk-seconds", "120",
        "--context", "Team standup about the launcher",
        "-o", "/Users/me/Recordings/meeting.vibevoice.txt",
    ])
}

@Test
func testVibevoiceContextOmittedWhenNil() {
    let command = CommandBuilder.command(for: .appleSiliconLocal, input: input, repoRoot: repoRoot)

    #expect(!command.arguments.contains("--context"))
}

@Test
func testOutputFileMatchesOutputArgument() {
    let fast = CommandBuilder.command(for: .cloud, input: input, repoRoot: repoRoot)
    let speakers = CommandBuilder.command(
        for: .privateLocalWithSpeakers,
        input: input,
        repoRoot: repoRoot
    )
    let vibevoice = CommandBuilder.command(for: .appleSiliconLocal, input: input, repoRoot: repoRoot)

    #expect(fast.outputFile.path == "/Users/me/Recordings/meeting.txt")
    #expect(speakers.outputFile.path == "/Users/me/Recordings/meeting.spk.txt")
    #expect(vibevoice.outputFile.path == "/Users/me/Recordings/meeting.vibevoice.txt")
}

@Test
func testCustomWhisperModelPath() {
    let command = CommandBuilder.command(
        for: .privateLocal,
        input: input,
        repoRoot: repoRoot,
        whisperModelPath: "/models/ggml-large-v3.bin"
    )

    #expect(command.arguments == [
        "run", "/Users/me/utils/audio_transcribe_whisper.py",
        "/Users/me/Recordings/meeting.m4a",
        "--format", "txt",
        "--large-model", "/models/ggml-large-v3.bin",
        "-o", "/Users/me/Recordings/meeting.txt",
    ])
}

@Test
func testDefaultWhisperModelPathOmitted() {
    let plain = CommandBuilder.command(for: .privateLocal, input: input, repoRoot: repoRoot)
    let speakers = CommandBuilder.command(
        for: .privateLocalWithSpeakers,
        input: input,
        repoRoot: repoRoot
    )

    #expect(!plain.arguments.contains("--large-model"))
    #expect(!speakers.arguments.contains("--large-model"))
}

@Test
func testFormatOptionsAndSuffixes() {
    let whisper = CommandBuilder.command(
        for: .privateLocal, input: input, repoRoot: repoRoot, format: .srt
    )
    #expect(whisper.arguments.contains("srt"))
    #expect(whisper.outputFile.lastPathComponent == "meeting.srt")

    let speakers = CommandBuilder.command(
        for: .privateLocalWithSpeakers, input: input, repoRoot: repoRoot, format: .markdown
    )
    #expect(speakers.arguments.contains("--diarization"))
    #expect(speakers.arguments.contains("md"))
    #expect(speakers.outputFile.lastPathComponent == "meeting.spk.md")

    let cloud = CommandBuilder.command(
        for: .cloud, input: input, repoRoot: repoRoot, format: .markdown
    )
    #expect(cloud.arguments.contains("--format"))
    #expect(cloud.outputFile.lastPathComponent == "meeting.md")

    let vibe = CommandBuilder.command(
        for: .appleSiliconLocal, input: input, repoRoot: repoRoot,
        format: .speakerText, keepVibeVoiceJSON: true
    )
    #expect(vibe.arguments.contains("diarized-txt"))
    #expect(vibe.arguments.contains("--keep-json"))
    #expect(vibe.outputFiles.map(\.lastPathComponent) ==
        ["meeting.vibevoice.spk.txt", "meeting.vibevoice.json"])
}

@Test
func testJSONReexportDoesNotInvokeTranscriptionModel() {
    let json = URL(fileURLWithPath: "/Users/me/Recordings/meeting.vibevoice.json")
    let command = CommandBuilder.command(
        for: .appleSiliconLocal, input: json, repoRoot: repoRoot, format: .markdown,
        keepVibeVoiceJSON: true
    )
    #expect(command.executable == "python3")
    #expect(command.arguments == [
        "/Users/me/utils/audio_transcribe_vibevoice.py",
        "--from-json", json.path, "--format", "md",
        "-o", "/Users/me/Recordings/meeting.vibevoice.md",
    ])
    #expect(command.outputFiles.count == 1)
}
