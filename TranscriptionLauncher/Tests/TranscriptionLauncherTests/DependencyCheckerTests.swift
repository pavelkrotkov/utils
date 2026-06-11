import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func reportsEverythingMissingForEmptyEnvironment() {
    let items = DependencyChecker.check(environment: [:])

    #expect(items.map(\.name) == ["ffmpeg", "uv", "whisper-cpp", "OPENAI_API_KEY", "HF_TOKEN"])
    #expect(items.allSatisfy { !$0.isAvailable })
    #expect(items.allSatisfy { $0.resolvedPath == nil })
}

@Test
func reportsExecutableFoundOnPathWithResolvedLocation() throws {
    try withTemporaryDirectory { directoryURL in
        let ffmpeg = directoryURL.appendingPathComponent("ffmpeg")
        FileManager.default.createFile(
            atPath: ffmpeg.path(percentEncoded: false),
            contents: Data("#!/bin/sh\n".utf8),
            attributes: [.posixPermissions: 0o755]
        )

        let items = DependencyChecker.check(
            environment: ["PATH": directoryURL.path(percentEncoded: false)]
        )

        let ffmpegItem = try #require(items.first { $0.name == "ffmpeg" })
        #expect(ffmpegItem.isAvailable)
        #expect(ffmpegItem.resolvedPath == ffmpeg.path(percentEncoded: false))
        #expect(ffmpegItem.requirement == .localPresets)

        let uvItem = try #require(items.first { $0.name == "uv" })
        #expect(!uvItem.isAvailable)
    }
}

@Test
func reportsAPIKeyAvailableWhenSet() throws {
    let items = DependencyChecker.check(environment: ["OPENAI_API_KEY": "sk-test"])

    let keyItem = try #require(items.first { $0.name == "OPENAI_API_KEY" })
    #expect(keyItem.isAvailable)
    #expect(keyItem.requirement == .cloudPresets)
    #expect(keyItem.resolvedPath == nil)
}

@Test
func treatsBlankAPIKeyAsMissing() throws {
    let items = DependencyChecker.check(environment: ["OPENAI_API_KEY": "  \n"])

    let keyItem = try #require(items.first { $0.name == "OPENAI_API_KEY" })
    #expect(!keyItem.isAvailable)
}

@Test
func prefersWhisperCppWhenBothBinariesArePresent() throws {
    try withTemporaryDirectory { directoryURL in
        let whisperCpp = makeExecutable(named: "whisper-cpp", in: directoryURL)
        _ = makeExecutable(named: "whisper-cli", in: directoryURL)

        let items = DependencyChecker.check(
            environment: ["PATH": directoryURL.path(percentEncoded: false)]
        )

        let whisperItem = try #require(items.first { $0.name == "whisper-cpp" })
        #expect(whisperItem.isAvailable)
        #expect(whisperItem.resolvedPath == whisperCpp.path(percentEncoded: false))
    }
}

@Test
func fallsBackToWhisperCliWhenWhisperCppIsMissing() throws {
    try withTemporaryDirectory { directoryURL in
        let whisperCli = makeExecutable(named: "whisper-cli", in: directoryURL)

        let items = DependencyChecker.check(
            environment: ["PATH": directoryURL.path(percentEncoded: false)]
        )

        let whisperItem = try #require(items.first { $0.name == "whisper-cpp" })
        #expect(whisperItem.isAvailable)
        #expect(whisperItem.resolvedPath == whisperCli.path(percentEncoded: false))
        #expect(whisperItem.requirement == .localPresets)
    }
}

@Test
func reportsHFTokenAsSpeakerDiarizationRequirement() throws {
    let items = DependencyChecker.check(environment: ["HF_TOKEN": "hf_test"])

    let tokenItem = try #require(items.first { $0.name == "HF_TOKEN" })
    #expect(tokenItem.isAvailable)
    #expect(tokenItem.requirement == .speakerDiarization)
    #expect(tokenItem.resolvedPath == nil)
}

private func makeExecutable(named name: String, in directoryURL: URL) -> URL {
    let executableURL = directoryURL.appendingPathComponent(name)
    FileManager.default.createFile(
        atPath: executableURL.path(percentEncoded: false),
        contents: Data("#!/bin/sh\n".utf8),
        attributes: [.posixPermissions: 0o755]
    )
    return executableURL
}

private func withTemporaryDirectory(_ body: (URL) throws -> Void) throws {
    let directoryURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("DependencyCheckerTests-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: directoryURL)
    }

    try body(directoryURL.standardizedFileURL)
}
