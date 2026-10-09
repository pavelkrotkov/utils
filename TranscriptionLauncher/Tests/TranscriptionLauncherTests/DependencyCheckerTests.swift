import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func selectedPresetsOnlyRequireTheirOwnToolsAndCredentials() {
    let local = DependencyChecker.check(preset: .privateLocal, environment: [:], repoRoot: nil, osMajorVersion: 14)
    let speakers = DependencyChecker.check(preset: .privateLocalWithSpeakers, environment: [:], repoRoot: nil, osMajorVersion: 14)
    let cloud = DependencyChecker.check(preset: .cloud, environment: [:], repoRoot: nil, osMajorVersion: 14)

    #expect(local.map(\.name) == ["audio_transcribe_whisper.py", "macOS 14+", "audio_common.py", "audio_segments.py", "audio_transcript.py", "uv", "ffmpeg", "whisper-cpp / whisper-cli", "Whisper model"])
    #expect(speakers.map(\.name) == local.map(\.name) + ["HF_TOKEN"])
    #expect(cloud.map(\.name) == ["audio_transcribe_openai.sh", "macOS 14+", "curl", "jq", "OPENAI_API_KEY"])
    #expect(local.first { $0.name == "Whisper model" }?.resolvedPath?.hasSuffix("ggml-large-v3-turbo-q8_0.bin") == true)
    #expect(!local.contains { $0.name == "OPENAI_API_KEY" || $0.name == "HF_TOKEN" })
}

@Test
func whisperCliFallbackAndModelPathAreValidatedBeforeRun() throws {
    try withTemporaryDirectory { root in
        let bin = root.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        let cli = try makeExecutable("whisper-cli", at: bin)
        _ = try makeExecutable("uv", at: bin)
        _ = try makeExecutable("ffmpeg", at: bin)
        try "script".write(to: root.appendingPathComponent("audio_transcribe_whisper.py"), atomically: true, encoding: .utf8)
        try installLocalModules(in: root)
        let model = root.appendingPathComponent("model.bin")
        try Data([1, 2, 3]).write(to: model)
        let env = ["PATH": bin.path]
        let check = { DependencyChecker.check(
            preset: .privateLocal, environment: env, repoRoot: root,
            whisperModelPath: model.path, osMajorVersion: 14
        ) }

        #expect(check().allSatisfy { $0.isAvailable })
        let missingModule = root.appendingPathComponent("audio_segments.py")
        try FileManager.default.removeItem(at: missingModule)
        #expect(check().first { $0.name == "audio_segments.py" }?.isAvailable == false)
        try Data([1]).write(to: missingModule)
        #expect(check().first { $0.name == "whisper-cpp / whisper-cli" }?.resolvedPath == cli.path)
        _ = try makeExecutable("whisper-cpp", at: bin)
        #expect(check().first { $0.name == "whisper-cpp / whisper-cli" }?.resolvedPath == bin.appendingPathComponent("whisper-cpp").path)

        try Data().write(to: model)
        #expect(check().first { $0.name == "Whisper model" }?.isAvailable == false)
        try FileManager.default.removeItem(at: model)
        #expect(check().first { $0.name == "Whisper model" }?.isAvailable == false)
        #expect(DependencyChecker.check(preset: .privateLocal, environment: env, repoRoot: root, whisperModelPath: "~/model.bin", osMajorVersion: 14)
            .first { $0.name == "Whisper model" }?.isAvailable == false)
    }
}

@Test
func missingPresetScriptAndCloudLargeFileAreReported() throws {
    try withTemporaryDirectory { root in
        let script = root.appendingPathComponent("audio_transcribe_openai.sh")
        try "#!/bin/sh\n".write(to: script, atomically: true, encoding: .utf8)
        let cloud = { (file: URL?) in DependencyChecker.check(
            preset: .cloud, environment: ["OPENAI_API_KEY": "key"],
            repoRoot: root, inputFile: file, osMajorVersion: 14
        ) }
        #expect(cloud(nil).first { $0.name == "audio_transcribe_openai.sh" }?.isAvailable == false)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        #expect(cloud(nil).first { $0.name == "audio_transcribe_openai.sh" }?.isAvailable == true)
        #expect(!cloud(nil).contains { $0.name == "ffmpeg" })

        let input = root.appendingPathComponent("large.m4a")
        #expect(FileManager.default.createFile(atPath: input.path, contents: nil))
        let handle = try FileHandle(forWritingTo: input)
        try handle.truncate(atOffset: 26 * 1024 * 1024)
        try handle.close()
        #expect(cloud(input).contains { $0.name == "ffmpeg" && !$0.isAvailable })
        #expect(DependencyChecker.check(preset: .privateLocal, environment: [:], repoRoot: root, osMajorVersion: 14)
            .first { $0.name == "audio_transcribe_whisper.py" }?.isAvailable == false)
    }
}

@Test
func vibeVoiceNeedsNativeAppleSiliconAndCompleteCachedWeights() throws {
    try withTemporaryDirectory { root in
        let bin = root.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        _ = try makeExecutable("uv", at: bin)
        _ = try makeExecutable("ffmpeg", at: bin)
        try "script".write(to: root.appendingPathComponent("audio_transcribe_vibevoice.py"), atomically: true, encoding: .utf8)
        try installLocalModules(in: root)
        let hub = root.appendingPathComponent("huggingface/hub", isDirectory: true)
        let snapshot = hub.appendingPathComponent("models--mlx-community--VibeVoice-ASR-4bit/snapshots/test", isDirectory: true)
        try FileManager.default.createDirectory(at: snapshot, withIntermediateDirectories: true)
        let env = ["PATH": bin.path, "HF_HUB_CACHE": hub.path]
        let check = { (arm: Bool) in DependencyChecker.check(
            preset: .appleSiliconLocal, environment: env, repoRoot: root,
            osMajorVersion: 14, isAppleSilicon: arm
        ) }
        #expect(check(true).first { $0.name == "VibeVoice model (5.7 GB)" }?.isAvailable == false)
        let blobs = root.appendingPathComponent("blobs", isDirectory: true)
        try FileManager.default.createDirectory(at: blobs, withIntermediateDirectories: true)
        for name in ["config.json", "model.safetensors.index.json", "model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"] {
            let blob = blobs.appendingPathComponent(name)
            try Data([1]).write(to: blob)
            try FileManager.default.createSymbolicLink(at: snapshot.appendingPathComponent(name), withDestinationURL: blob)
        }
        #expect(check(true).allSatisfy { $0.isAvailable })
        #expect(check(false).first { $0.name == "Apple Silicon" }?.isAvailable == false)
        try FileManager.default.removeItem(at: snapshot.appendingPathComponent("model-00002-of-00002.safetensors"))
        #expect(check(true).first { $0.name == "VibeVoice model (5.7 GB)" }?.isAvailable == false)
    }
}

private func installLocalModules(in root: URL) throws {
    for name in ["audio_common.py", "audio_segments.py", "audio_transcript.py"] {
        try Data([1]).write(to: root.appendingPathComponent(name))
    }
}

private func makeExecutable(_ name: String, at root: URL) throws -> URL {
    let url = root.appendingPathComponent(name)
    try "#!/bin/sh\n".write(to: url, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    return url
}

private func withTemporaryDirectory(_ body: (URL) throws -> Void) throws {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("DependencyChecker-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    try body(root.standardizedFileURL)
}
