import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func resolvesBareNameFromPathDirectories() throws {
    try withTemporaryDirectory { directoryURL in
        let binDirectory = directoryURL.appendingPathComponent("bin", isDirectory: true)
        try FileManager.default.createDirectory(at: binDirectory, withIntermediateDirectories: true)
        let tool = binDirectory.appendingPathComponent("mytool")
        FileManager.default.createFile(
            atPath: tool.path,
            contents: Data("#!/bin/sh\n".utf8),
            attributes: [.posixPermissions: 0o755]
        )

        let resolved = ExecutableResolver.resolve(
            "mytool",
            environment: ["PATH": "/nonexistent:\(binDirectory.path)"]
        )

        #expect(resolved?.path == tool.path)
    }
}

@Test
func returnsNilForBareNameAbsentFromPath() throws {
    try withTemporaryDirectory { directoryURL in
        let resolved = ExecutableResolver.resolve(
            "mytool",
            environment: ["PATH": directoryURL.path]
        )

        #expect(resolved == nil)
    }
}

@Test
func skipsNonExecutableFiles() throws {
    try withTemporaryDirectory { directoryURL in
        let tool = directoryURL.appendingPathComponent("mytool")
        FileManager.default.createFile(
            atPath: tool.path,
            contents: Data(),
            attributes: [.posixPermissions: 0o644]
        )

        let resolved = ExecutableResolver.resolve(
            "mytool",
            environment: ["PATH": directoryURL.path]
        )

        #expect(resolved == nil)
    }
}

@Test
func skipsDirectoriesNamedLikeExecutable() throws {
    try withTemporaryDirectory { directoryURL in
        try FileManager.default.createDirectory(
            at: directoryURL.appendingPathComponent("mytool", isDirectory: true),
            withIntermediateDirectories: true
        )

        let resolved = ExecutableResolver.resolve(
            "mytool",
            environment: ["PATH": directoryURL.path]
        )

        #expect(resolved == nil)
    }
}

@Test
func usesPathContainingExecutableDirectly() {
    let resolved = ExecutableResolver.resolve("/bin/sh", environment: [:])

    #expect(resolved == URL(fileURLWithPath: "/bin/sh", isDirectory: false))
}

@Test
func returnsNilForMissingAbsolutePath() {
    let resolved = ExecutableResolver.resolve("/nonexistent/tool", environment: [:])

    #expect(resolved == nil)
}

@Test
func returnsNilForEmptyName() {
    #expect(ExecutableResolver.resolve("", environment: ["PATH": "/bin"]) == nil)
}

private func withTemporaryDirectory(_ body: (URL) throws -> Void) throws {
    let directoryURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("ExecutableResolverTests-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: directoryURL)
    }

    try body(directoryURL.standardizedFileURL)
}
