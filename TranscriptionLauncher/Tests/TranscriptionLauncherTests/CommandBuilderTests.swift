import Foundation
import Testing
import TranscriptionLauncherLib

private let repoRoot = URL(fileURLWithPath: "/Users/me/utils", isDirectory: true)
private let input = URL(fileURLWithPath: "/Users/me/Recordings/meeting.m4a")

@Test
func testFastCloudPreset() {
    let command = CommandBuilder.command(for: .fastCloud, input: input, repoRoot: repoRoot)

    #expect(command.executable == "/Users/me/utils/audio_transcribe_openai.sh")
    #expect(command.arguments == [
        "--model", "gpt-4o-mini-transcribe",
        "/Users/me/Recordings/meeting.m4a",
        "/Users/me/Recordings/meeting.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testBestCloudPreset() {
    let command = CommandBuilder.command(for: .bestCloud, input: input, repoRoot: repoRoot)

    #expect(command.executable == "/Users/me/utils/audio_transcribe_openai.sh")
    #expect(command.arguments == [
        "--model", "gpt-4o-transcribe",
        "/Users/me/Recordings/meeting.m4a",
        "/Users/me/Recordings/meeting.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testCompatibleCloudPreset() {
    let command = CommandBuilder.command(for: .compatibleCloud, input: input, repoRoot: repoRoot)

    #expect(command.executable == "/Users/me/utils/audio_transcribe_openai.sh")
    #expect(command.arguments == [
        "--model", "whisper-1",
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
        "--diarization",
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
        "--format", "txt",
        "-o", "/Users/me/Recordings/meeting.vibevoice.txt",
    ])
    #expect(command.workingDirectory == repoRoot)
}

@Test
func testSpacesInFilename() {
    // Arguments are an array, not a shell string — spaces are safe
    let spacedInput = URL(fileURLWithPath: "/Users/me/My Recordings/team sync.m4a")

    let command = CommandBuilder.command(for: .fastCloud, input: spacedInput, repoRoot: repoRoot)

    #expect(command.arguments == [
        "--model", "gpt-4o-mini-transcribe",
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
func testVibevoiceContextInjected() {
    let command = CommandBuilder.command(
        for: .appleSiliconLocal,
        input: input,
        repoRoot: repoRoot,
        vibevoiceContext: "Team standup about the launcher"
    )

    #expect(command.arguments == [
        "run", "/Users/me/utils/audio_transcribe_vibevoice.py",
        "/Users/me/Recordings/meeting.m4a",
        "--format", "txt",
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
    let fast = CommandBuilder.command(for: .fastCloud, input: input, repoRoot: repoRoot)
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
