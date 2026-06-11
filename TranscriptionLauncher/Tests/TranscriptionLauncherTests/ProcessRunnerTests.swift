import Darwin
import Foundation
import Testing
import TranscriptionLauncherLib

private let shellEnvironment = ["PATH": "/usr/bin:/bin"]

@Test
@MainActor
func testSuccessfulRunProducesOutputFile() async throws {
    try await withTemporaryDirectory { directoryURL in
        let output = directoryURL.appendingPathComponent("out.txt")
        let runner = ProcessRunner()
        let command = shCommand(
            "echo working; echo transcript > \"$0\"",
            arguments: [output.path],
            workingDirectory: directoryURL,
            outputFile: output
        )

        let resultURL = try await runner.run(command: command, environment: shellEnvironment)

        #expect(resultURL == output)
        #expect(FileManager.default.fileExists(atPath: output.path))
        #expect(runner.logLines.contains("working"))
        #expect(!runner.isRunning)
    }
}

@Test
@MainActor
func testNonZeroExitThrowsClassifiedError() async throws {
    try await withTemporaryDirectory { directoryURL in
        let runner = ProcessRunner()
        let command = shCommand(
            "echo 'Error: OPENAI_API_KEY is not set.' >&2; exit 1",
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )

        await #expect(throws: TranscriptionError.missingAPIKey("OPENAI_API_KEY")) {
            try await runner.run(command: command, environment: shellEnvironment)
        }
        #expect(!runner.isRunning)
    }
}

@Test
@MainActor
func testUnresolvedExecutableThrowsMissingDependency() async throws {
    try await withTemporaryDirectory { directoryURL in
        let runner = ProcessRunner()
        let command = TranscriptionCommand(
            executable: "uv",
            arguments: ["run"],
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )

        await #expect(throws: TranscriptionError.missingDependency("uv")) {
            try await runner.run(command: command, environment: ["PATH": directoryURL.path])
        }
    }
}

@Test
@MainActor
func testCancellationTerminatesProcess() async throws {
    try await withTemporaryDirectory { directoryURL in
        let runner = ProcessRunner()
        let command = shCommand(
            "echo started; exec sleep 30",
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )

        let runTask = Task {
            try await runner.run(command: command, environment: shellEnvironment)
        }
        #expect(try await waitUntil { runner.logLines.contains("started") })
        #expect(runner.isRunning)

        let cancelledAt = ContinuousClock.now
        runner.cancel()
        await #expect(throws: CancellationError.self) {
            try await runTask.value
        }
        #expect(ContinuousClock.now - cancelledAt < .seconds(10))
        #expect(!runner.isRunning)
    }
}

@Test
@MainActor
func testCancellationTerminatesChildProcessTree() async throws {
    try await withTemporaryDirectory { directoryURL in
        let runner = ProcessRunner()
        // A parent that owns a child and forwards SIGTERM, the same shape
        // as `uv run` wrapping the Python script.
        let script = """
        sleep 300 &
        child=$!
        echo "child=$child"
        trap 'kill "$child" 2>/dev/null' TERM
        wait "$child"
        """
        let command = shCommand(
            script,
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )

        let runTask = Task {
            try await runner.run(command: command, environment: shellEnvironment)
        }
        let reportedPID = try await reportedChildPID(in: runner)
        let childPID = try #require(reportedPID)

        runner.cancel()
        await #expect(throws: CancellationError.self) {
            try await runTask.value
        }
        #expect(try await processGone(childPID))
    }
}

@Test
@MainActor
func testStderrStreamedToCallback() async throws {
    try await withTemporaryDirectory { directoryURL in
        let flag = directoryURL.appendingPathComponent("flag")
        let output = directoryURL.appendingPathComponent("out.txt")
        let runner = ProcessRunner()
        let command = shCommand(
            "echo ready >&2; until [ -e \"$0\" ]; do sleep 0.05; done; echo done > \"$1\"",
            arguments: [flag.path, output.path],
            workingDirectory: directoryURL,
            outputFile: output
        )

        let collector = LineCollector()
        let runTask = Task {
            try await runner.run(command: command, environment: shellEnvironment) { line in
                collector.lines.append(line)
            }
        }

        // The line must arrive while the process is still running, i.e. it
        // is streamed rather than collected at exit.
        #expect(try await waitUntil { collector.lines.contains("ready") })
        #expect(runner.isRunning)

        FileManager.default.createFile(atPath: flag.path, contents: nil)
        _ = try await runTask.value

        #expect(runner.logLines.contains("ready"))
    }
}

@Test
@MainActor
func testProgressEventsExtracted() async throws {
    try await withTemporaryDirectory { directoryURL in
        let output = directoryURL.appendingPathComponent("out.txt")
        let runner = ProcessRunner()
        let script = """
        echo 'INFO: whisper-cpp ASR started' >&2
        echo 'INFO: whisper-cpp ASR:  45.2%, elapsed 01:23, ETA 01:42' >&2
        echo transcript > "$0"
        """
        let command = shCommand(
            script,
            arguments: [output.path],
            workingDirectory: directoryURL,
            outputFile: output
        )

        _ = try await runner.run(command: command, environment: shellEnvironment)

        #expect(runner.progress == ProgressEvent(
            stage: "whisper-cpp ASR",
            percent: 45.2,
            detail: "elapsed 01:23, ETA 01:42"
        ))
    }
}

@Test
@MainActor
func testMissingOutputFileAfterSuccessThrows() async throws {
    try await withTemporaryDirectory { directoryURL in
        let output = directoryURL.appendingPathComponent("never.txt")
        let runner = ProcessRunner()
        let command = shCommand(
            "true",
            workingDirectory: directoryURL,
            outputFile: output
        )

        await #expect(throws: ProcessRunnerError.outputFileMissing(output)) {
            try await runner.run(command: command, environment: shellEnvironment)
        }
    }
}

@Test
@MainActor
func testSecondRunWhileRunningThrows() async throws {
    try await withTemporaryDirectory { directoryURL in
        let runner = ProcessRunner()
        let first = shCommand(
            "echo started; exec sleep 30",
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )
        let second = shCommand(
            "true",
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )

        let runTask = Task {
            try await runner.run(command: first, environment: shellEnvironment)
        }
        #expect(try await waitUntil { runner.isRunning })

        await #expect(throws: ProcessRunnerError.alreadyRunning) {
            try await runner.run(command: second, environment: shellEnvironment)
        }

        runner.cancel()
        await #expect(throws: CancellationError.self) {
            try await runTask.value
        }
    }
}

private let uvExecutableURL = ExecutableResolver.resolve(
    "uv",
    environment: ProcessInfo.processInfo.environment
)

// CI runners do not have `uv`, so this runs only on developer machines;
// it verifies the issue's explicit requirement that terminating `uv`
// reaches the Python child.
@Test(.enabled(if: uvExecutableURL != nil))
@MainActor
func testCancellationTerminatesPythonChildUnderUV() async throws {
    try await withTemporaryDirectory { directoryURL in
        let runner = ProcessRunner()
        let command = TranscriptionCommand(
            executable: "uv",
            arguments: [
                "run", "--no-project", "python3", "-c",
                "import os, time; print(f'child={os.getpid()}', flush=True); time.sleep(300)",
            ],
            workingDirectory: directoryURL,
            outputFile: directoryURL.appendingPathComponent("never.txt")
        )

        let runTask = Task {
            try await runner.run(
                command: command,
                environment: ProcessInfo.processInfo.environment
            )
        }
        // Generous timeout: uv may have to provision an interpreter first.
        let reportedPID = try await reportedChildPID(in: runner, timeout: .seconds(60))
        let childPID = try #require(reportedPID)

        runner.cancel()
        await #expect(throws: CancellationError.self) {
            try await runTask.value
        }
        #expect(try await processGone(childPID))
    }
}

@MainActor
private final class LineCollector {
    var lines: [String] = []
}

private func shCommand(
    _ script: String,
    arguments: [String] = [],
    workingDirectory: URL,
    outputFile: URL
) -> TranscriptionCommand {
    TranscriptionCommand(
        executable: "/bin/sh",
        arguments: ["-c", script] + arguments,
        workingDirectory: workingDirectory,
        outputFile: outputFile
    )
}

@MainActor
private func withTemporaryDirectory(
    _ body: @MainActor (URL) async throws -> Void
) async throws {
    let directoryURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("ProcessRunnerTests-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: directoryURL)
    }

    try await body(directoryURL.standardizedFileURL)
}

@MainActor
private func waitUntil(
    timeout: Duration = .seconds(10),
    _ condition: @MainActor () -> Bool
) async throws -> Bool {
    let deadline = ContinuousClock.now + timeout
    while ContinuousClock.now < deadline {
        if condition() {
            return true
        }
        try await Task.sleep(for: .milliseconds(20))
    }
    return false
}

/// Returns the PID a test script reported via a `child=<pid>` line.
@MainActor
private func reportedChildPID(
    in runner: ProcessRunner,
    timeout: Duration = .seconds(10)
) async throws -> pid_t? {
    var childPID: pid_t?
    _ = try await waitUntil(timeout: timeout) {
        guard let line = runner.logLines.first(where: { $0.hasPrefix("child=") }) else {
            return false
        }
        childPID = pid_t(line.dropFirst("child=".count))
        return childPID != nil
    }
    return childPID
}

private func processGone(
    _ pid: pid_t,
    timeout: Duration = .seconds(10)
) async throws -> Bool {
    let deadline = ContinuousClock.now + timeout
    while ContinuousClock.now < deadline {
        if kill(pid, 0) == -1 && errno == ESRCH {
            return true
        }
        try await Task.sleep(for: .milliseconds(50))
    }
    return false
}
