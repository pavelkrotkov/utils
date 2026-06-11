import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func appMetadataProvidesDefaultDisplayName() {
    let metadata = AppMetadata()

    #expect(metadata.displayName == "Transcription Launcher")
}

@Test
func environmentSnapshotParsesBasicOutput() {
    let values = EnvironmentSnapshot.parse("HOME=/Users/me\nPATH=/usr/bin")

    #expect(values == [
        "HOME": "/Users/me",
        "PATH": "/usr/bin",
    ])
}

@Test
func environmentSnapshotParsesValueContainingEquals() {
    let values = EnvironmentSnapshot.parse("PROMPT=foo=bar=baz")

    #expect(values["PROMPT"] == "foo=bar=baz")
}

@Test
func environmentSnapshotParsesNullDelimitedOutput() {
    let values = EnvironmentSnapshot.parse("FIRST=one\0MULTILINE=line 1\nline 2\0")

    #expect(values == [
        "FIRST": "one",
        "MULTILINE": "line 1\nline 2",
    ])
}

@Test
func environmentSnapshotParsesEmptyValue() {
    let values = EnvironmentSnapshot.parse("EMPTY_VAR=")

    #expect(values["EMPTY_VAR"] == "")
}

@Test
func environmentSnapshotSkipsMalformedLines() {
    let values = EnvironmentSnapshot.parse("GOOD=value\nno_equals_here\nALSO_GOOD=123")

    #expect(values == [
        "GOOD": "value",
        "ALSO_GOOD": "123",
    ])
}

@Test
func environmentSnapshotSkipsShellFunctionExports() {
    let values = EnvironmentSnapshot.parse(
        """
        GOOD=value
        BASH_FUNC_foo%%=() {
        ALSO_GOOD=123
        """
    )

    #expect(values == [
        "GOOD": "value",
        "ALSO_GOOD": "123",
    ])
}

@Test
func repoDetectorFindsRepoByMarkerFile() throws {
    try withTemporaryDirectory { repoRoot in
        let nestedURL = repoRoot
            .appendingPathComponent("one", isDirectory: true)
            .appendingPathComponent("two", isDirectory: true)
        try FileManager.default.createDirectory(at: nestedURL, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: repoRoot.appendingPathComponent("audio_transcribe_openai.sh").path(percentEncoded: false),
            contents: Data()
        )

        let foundURL = RepoDetector.findRepoRoot(startingFrom: nestedURL)

        #expect(foundURL == repoRoot.standardizedFileURL)
    }
}

@Test
func repoDetectorReturnsNilWhenNotInRepo() throws {
    try withTemporaryDirectory { directoryURL in
        let foundURL = RepoDetector.findRepoRoot(startingFrom: directoryURL)

        #expect(foundURL == nil)
    }
}

@Test
func repoDetectorDoesNotUseGenericPyprojectAsRepoMarker() throws {
    try withTemporaryDirectory { directoryURL in
        FileManager.default.createFile(
            atPath: directoryURL.appendingPathComponent("pyproject.toml").path(percentEncoded: false),
            contents: Data()
        )

        let foundURL = RepoDetector.findRepoRoot(startingFrom: directoryURL)

        #expect(foundURL == nil)
    }
}

@Test
func repoDetectorValidatesExactRepoRoot() throws {
    try withTemporaryDirectory { repoRoot in
        let nestedURL = repoRoot.appendingPathComponent("nested", isDirectory: true)
        try FileManager.default.createDirectory(at: nestedURL, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: repoRoot.appendingPathComponent("audio_common.py").path(percentEncoded: false),
            contents: Data()
        )

        #expect(RepoDetector.isRepoRoot(repoRoot))
        #expect(!RepoDetector.isRepoRoot(nestedURL))
    }
}

@Test
func repoDetectorStopsAtFilesystemRoot() {
    let foundURL = RepoDetector.findRepoRoot(
        startingFrom: URL(fileURLWithPath: "/", isDirectory: true)
    )

    #expect(foundURL == nil)
}

@Test
func repoDetectorFindsRepoWhenAppIsDirectlyInside() throws {
    try withTemporaryDirectory { repoRoot in
        FileManager.default.createFile(
            atPath: repoRoot.appendingPathComponent("audio_transcribe_openai.sh").path(percentEncoded: false),
            contents: Data()
        )

        let foundURL = RepoDetector.findRepoRoot(startingFrom: repoRoot)

        #expect(foundURL == repoRoot.standardizedFileURL)
    }
}

@Test
func repoDetectorResolvesSymlinkedStartPath() throws {
    try withTemporaryDirectory { tempRoot in
        let repoRoot = tempRoot.appendingPathComponent("repo", isDirectory: true)
        let nestedURL = repoRoot.appendingPathComponent("nested", isDirectory: true)
        let linkURL = tempRoot.appendingPathComponent("repo-link", isDirectory: true)
        try FileManager.default.createDirectory(at: nestedURL, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: repoRoot.appendingPathComponent("audio_transcribe_openai.sh").path(percentEncoded: false),
            contents: Data()
        )
        try FileManager.default.createSymbolicLink(at: linkURL, withDestinationURL: repoRoot)

        let foundURL = RepoDetector.findRepoRoot(
            startingFrom: linkURL.appendingPathComponent("nested", isDirectory: true)
        )

        #expect(foundURL == repoRoot.standardizedFileURL)
    }
}

@Test
func repoDetectorIgnoresMarkerDirectory() throws {
    try withTemporaryDirectory { repoRoot in
        try FileManager.default.createDirectory(
            at: repoRoot.appendingPathComponent("audio_transcribe_openai.sh", isDirectory: true),
            withIntermediateDirectories: true
        )

        let foundURL = RepoDetector.findRepoRoot(startingFrom: repoRoot)

        #expect(foundURL == nil)
    }
}

@Test
func repoDetectorDoesNotReturnUnreadableDirectory() throws {
    try withTemporaryDirectory { repoRoot in
        FileManager.default.createFile(
            atPath: repoRoot.appendingPathComponent("audio_transcribe_openai.sh").path(percentEncoded: false),
            contents: Data()
        )
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o300],
            ofItemAtPath: repoRoot.path(percentEncoded: false)
        )
        defer {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o700],
                ofItemAtPath: repoRoot.path(percentEncoded: false)
            )
        }

        let foundURL = RepoDetector.findRepoRoot(startingFrom: repoRoot)

        #expect(foundURL == nil)
    }
}

private func withTemporaryDirectory(_ body: (URL) throws -> Void) throws {
    let directoryURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("RepoDetectorTests-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: directoryURL)
    }

    try body(directoryURL.standardizedFileURL)
}
