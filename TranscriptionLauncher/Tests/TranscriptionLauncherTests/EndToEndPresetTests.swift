import Foundation
import Testing
import TranscriptionLauncherLib

// End-to-end preset verification (#64): each test drives the real pipeline —
// CommandBuilder builds the command, ProcessRunner executes it — against a
// fixture repo whose transcription scripts are stubs that reproduce the real
// scripts' argument interfaces and error messages. `uv` is faked with a shim
// that executes the stub scripts with /bin/sh, so the `uv run <script>`
// presets exercise the same wrapper shape as production.

@Test
@MainActor
func endToEndEveryPresetWritesItsTranscriptNextToInput() async throws {
    let expectations: [(TranscriptionPreset, String)] = [
        (.fastCloud, "meeting.txt"),
        (.bestCloud, "meeting.txt"),
        (.compatibleCloud, "meeting.txt"),
        (.privateLocal, "meeting.txt"),
        (.privateLocalWithSpeakers, "meeting.spk.txt"),
        (.appleSiliconLocal, "meeting.vibevoice.txt"),
    ]

    for (preset, expectedName) in expectations {
        try await withE2EFixture { fixture in
            let input = try fixture.makeInput(named: "meeting.m4a")
            let expectedOutput = input.deletingLastPathComponent()
                .appendingPathComponent(expectedName, isDirectory: false)
            let command = CommandBuilder.command(
                for: preset,
                input: input,
                repoRoot: fixture.repoRoot
            )

            let result = try await ProcessRunner().run(
                command: command,
                environment: fixture.environment
            )

            #expect(result.path == expectedOutput.path)
            let transcript = try String(contentsOf: expectedOutput, encoding: .utf8)
            #expect(transcript.contains("transcript"))
        }
    }
}

@Test
@MainActor
func endToEndEdgeCaseFilenames() async throws {
    let expectations: [(input: String, output: String)] = [
        ("my recording (1).m4a", "my recording (1).txt"),
        ("café interview.m4a", "café interview.txt"),
        ("recording", "recording.txt"),
        ("my.podcast.ep3.m4a", "my.podcast.ep3.txt"),
    ]

    // Cover both execution paths: the shell script invoked directly
    // (fastCloud) and the Python script wrapped in `uv run` (privateLocal).
    for preset: TranscriptionPreset in [.fastCloud, .privateLocal] {
        for expectation in expectations {
            try await withE2EFixture { fixture in
                let input = try fixture.makeInput(named: expectation.input)
                let expectedOutput = input.deletingLastPathComponent()
                    .appendingPathComponent(expectation.output, isDirectory: false)
                let command = CommandBuilder.command(
                    for: preset,
                    input: input,
                    repoRoot: fixture.repoRoot
                )

                let result = try await ProcessRunner().run(
                    command: command,
                    environment: fixture.environment
                )

                #expect(result.path == expectedOutput.path)
                #expect(FileManager.default.fileExists(atPath: expectedOutput.path))
            }
        }
    }
}

@Test
@MainActor
func endToEndExistingOutputIsOverwritten() async throws {
    // The confirmation dialog before overwriting is UI behaviour
    // (LauncherModel.pendingOverwriteRun); once confirmed, the pipeline
    // must replace the stale transcript.
    try await withE2EFixture { fixture in
        let input = try fixture.makeInput(named: "meeting.m4a")
        let output = input.deletingLastPathComponent()
            .appendingPathComponent("meeting.txt", isDirectory: false)
        try "stale transcript".write(to: output, atomically: true, encoding: .utf8)
        let command = CommandBuilder.command(
            for: .fastCloud,
            input: input,
            repoRoot: fixture.repoRoot
        )

        _ = try await ProcessRunner().run(command: command, environment: fixture.environment)

        let transcript = try String(contentsOf: output, encoding: .utf8)
        #expect(!transcript.contains("stale"))
        #expect(transcript.contains("transcript"))
    }
}

@Test
@MainActor
func endToEndCancelledCloudRunLeavesNoOutput() async throws {
    try await withE2EFixture { fixture in
        // A cloud run that hangs before writing its output, like a stalled
        // upload; cancelling must stop it without leaving a partial file.
        try fixture.installScript(
            named: "audio_transcribe_openai.sh",
            body: """
            echo started
            exec sleep 300
            """
        )
        let input = try fixture.makeInput(named: "meeting.m4a")
        let runner = ProcessRunner()
        let command = CommandBuilder.command(
            for: .fastCloud,
            input: input,
            repoRoot: fixture.repoRoot
        )

        let runTask = Task {
            try await runner.run(command: command, environment: fixture.environment)
        }
        // Guarantees the child process is torn down even when the test
        // throws before the explicit cancel below.
        defer { runTask.cancel() }
        let sawStarted = try await waitUntil { runner.logLines.contains("started") }
        #expect(sawStarted)

        runner.cancel()
        await #expect(throws: CancellationError.self) {
            try await runTask.value
        }
        #expect(!FileManager.default.fileExists(atPath: command.outputFile.path))
    }
}

@Test
@MainActor
func endToEndCancelledUVRunCleansUpTempFiles() async throws {
    try await withE2EFixture { fixture in
        // A local whisper run that owns a temp file and removes it when
        // terminated; cancellation must reach the script through `uv` so
        // its cleanup trap runs.
        try fixture.installScript(
            named: "audio_transcribe_whisper.py",
            body: """
            tmp="$1.partial"
            : > "$tmp"
            echo "tmp=$tmp"
            trap 'rm -f "$tmp"; exit 143' TERM
            sleep 300 &
            wait $!
            """
        )
        let input = try fixture.makeInput(named: "meeting.m4a")
        let runner = ProcessRunner()
        let command = CommandBuilder.command(
            for: .privateLocal,
            input: input,
            repoRoot: fixture.repoRoot
        )
        let tempFile = URL(fileURLWithPath: input.path + ".partial", isDirectory: false)

        let runTask = Task {
            try await runner.run(command: command, environment: fixture.environment)
        }
        // Guarantees the child process is torn down even when the test
        // throws before the explicit cancel below.
        defer { runTask.cancel() }
        let tempCreated = try await waitUntil {
            runner.logLines.contains("tmp=\(tempFile.path)")
        }
        #expect(tempCreated)

        runner.cancel()
        await #expect(throws: CancellationError.self) {
            try await runTask.value
        }
        let tempRemoved = try await waitUntil {
            !FileManager.default.fileExists(atPath: tempFile.path)
        }
        #expect(tempRemoved)
    }
}

@Test
@MainActor
func endToEndScriptFailuresSurfaceFriendlyErrors() async throws {
    // Each stderr line is verbatim from the real script named in the
    // scenario; the classified error is what the UI renders as a friendly
    // message (ErrorPresentation).
    let scenarios: [(
        preset: TranscriptionPreset,
        script: String,
        stderr: String,
        expected: TranscriptionError
    )] = [
        (
            .fastCloud,
            "audio_transcribe_openai.sh",
            "Error: OPENAI_API_KEY is not set.",
            .missingAPIKey("OPENAI_API_KEY")
        ),
        (
            .privateLocalWithSpeakers,
            "audio_transcribe_whisper.py",
            "ERROR: Failed to load both pyannote/speaker-diarization pipelines.",
            .missingAPIKey("HF_TOKEN")
        ),
        (
            .privateLocal,
            "audio_transcribe_whisper.py",
            "ERROR: Whisper model not found: /models/ggml-large-v3.bin",
            .missingModel("/models/ggml-large-v3.bin")
        ),
        (
            .privateLocal,
            "audio_transcribe_whisper.py",
            "ERROR: ffmpeg binary not found or not executable: /opt/homebrew/bin/ffmpeg",
            .missingDependency("ffmpeg")
        ),
        (
            .appleSiliconLocal,
            "audio_transcribe_vibevoice.py",
            "ERROR: mlx-audio/VibeVoice-ASR is intended for Apple Silicon Macs (Darwin arm64).",
            .unsupportedHardware("VibeVoice requires Apple Silicon")
        ),
    ]

    for scenario in scenarios {
        try await withE2EFixture { fixture in
            try fixture.installScript(
                named: scenario.script,
                body: """
                echo '\(scenario.stderr)' >&2
                exit 1
                """
            )
            let input = try fixture.makeInput(named: "meeting.m4a")
            let command = CommandBuilder.command(
                for: scenario.preset,
                input: input,
                repoRoot: fixture.repoRoot
            )

            await #expect(throws: scenario.expected) {
                try await ProcessRunner().run(
                    command: command,
                    environment: fixture.environment
                )
            }
        }
    }
}

@Test
@MainActor
func endToEndMissingUVReportsMissingDependency() async throws {
    try await withE2EFixture { fixture in
        let input = try fixture.makeInput(named: "meeting.m4a")
        let command = CommandBuilder.command(
            for: .privateLocal,
            input: input,
            repoRoot: fixture.repoRoot
        )

        await #expect(throws: TranscriptionError.missingDependency("uv")) {
            try await ProcessRunner().run(
                command: command,
                environment: fixture.environmentWithoutUV
            )
        }
    }
}

@Test
@MainActor
func endToEndAPIErrorSurfacedWithDetail() async throws {
    try await withE2EFixture { fixture in
        try fixture.installScript(
            named: "audio_transcribe_openai.sh",
            body: """
            echo 'Error: OpenAI API request failed (HTTP 429).' >&2
            echo 'API error (insufficient_quota): You exceeded your current quota.' >&2
            exit 1
            """
        )
        let input = try fixture.makeInput(named: "meeting.m4a")
        let command = CommandBuilder.command(
            for: .fastCloud,
            input: input,
            repoRoot: fixture.repoRoot
        )

        let expected = TranscriptionError.apiError(
            "OpenAI API request failed (HTTP 429)."
                + " API error (insufficient_quota): You exceeded your current quota."
        )
        await #expect(throws: expected) {
            try await ProcessRunner().run(command: command, environment: fixture.environment)
        }
    }
}

@Test
@MainActor
func endToEndProgressEventsFlowThroughUVRun() async throws {
    try await withE2EFixture { fixture in
        try fixture.installScript(
            named: "audio_transcribe_whisper.py",
            body: """
            input="$1"; shift
            output=""
            while [ $# -gt 0 ]; do
              if [ "$1" = "-o" ]; then output="$2"; shift 2; else shift; fi
            done
            echo 'INFO: whisper-cpp ASR started' >&2
            echo 'INFO: whisper-cpp ASR:  45.2%, elapsed 01:23, ETA 01:42' >&2
            echo transcript > "$output"
            """
        )
        let input = try fixture.makeInput(named: "meeting.m4a")
        let runner = ProcessRunner()
        let command = CommandBuilder.command(
            for: .privateLocal,
            input: input,
            repoRoot: fixture.repoRoot
        )

        _ = try await runner.run(command: command, environment: fixture.environment)

        #expect(runner.progress == ProgressEvent(
            stage: "whisper-cpp ASR",
            percent: 45.2,
            detail: "elapsed 01:23, ETA 01:42"
        ))
    }
}

// MARK: - Fixture

@MainActor
private struct E2EFixture {
    let repoRoot: URL
    let binDirectory: URL
    let recordingsDirectory: URL

    /// PATH resolves the fake `uv` first, then the system shells the
    /// stub scripts need.
    var environment: [String: String] {
        ["PATH": "\(binDirectory.path):/usr/bin:/bin"]
    }

    var environmentWithoutUV: [String: String] {
        ["PATH": "/usr/bin:/bin"]
    }

    /// Replaces a stub transcription script in the fixture repo. The body
    /// runs under /bin/sh — directly for the `.sh` script, via the fake
    /// `uv` shim for the `.py` scripts.
    func installScript(named name: String, body: String) throws {
        try installExecutable(
            at: repoRoot.appendingPathComponent(name, isDirectory: false),
            contents: "#!/bin/sh\n\(body)\n"
        )
    }

    func makeInput(named name: String) throws -> URL {
        let url = recordingsDirectory.appendingPathComponent(name, isDirectory: false)
        try Data("fake audio".utf8).write(to: url)
        return url
    }
}

@MainActor
private func withE2EFixture(
    _ body: @MainActor (E2EFixture) async throws -> Void
) async throws {
    let rootURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("EndToEndPresetTests-\(UUID().uuidString)", isDirectory: true)
    let fixture = E2EFixture(
        repoRoot: rootURL.appendingPathComponent("repo", isDirectory: true),
        binDirectory: rootURL.appendingPathComponent("bin", isDirectory: true),
        recordingsDirectory: rootURL.appendingPathComponent("recordings", isDirectory: true)
    )
    for directory in [fixture.repoRoot, fixture.binDirectory, fixture.recordingsDirectory] {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
    defer {
        try? FileManager.default.removeItem(at: rootURL)
    }

    // Fake `uv run <script> <args...>`: executes the stub script with
    // /bin/sh so the `.py` stubs can be plain shell scripts while keeping
    // the wrapper-process shape of the real `uv`.
    try installExecutable(
        at: fixture.binDirectory.appendingPathComponent("uv", isDirectory: false),
        contents: """
        #!/bin/sh
        [ "$1" = "run" ] && shift
        script="$1"
        shift
        exec /bin/sh "$script" "$@"
        """
    )

    // Default success stubs mirror the real scripts' argument interfaces:
    // the OpenAI wrapper takes `--model NAME INPUT OUTPUT`, the Python
    // scripts take `INPUT [options] -o OUTPUT`.
    try fixture.installScript(
        named: "audio_transcribe_openai.sh",
        body: """
        [ "$1" = "--model" ] || { echo "Error: expected --model" >&2; exit 2; }
        model="$2"; input="$3"; output="$4"
        [ -f "$input" ] || { echo "Error: input not found: $input" >&2; exit 1; }
        printf 'transcript(%s)\\n' "$model" > "$output"
        """
    )
    for script in ["audio_transcribe_whisper.py", "audio_transcribe_vibevoice.py"] {
        try fixture.installScript(
            named: script,
            body: """
            input="$1"; shift
            output=""
            while [ $# -gt 0 ]; do
              if [ "$1" = "-o" ]; then output="$2"; shift 2; else shift; fi
            done
            [ -f "$input" ] || { echo "ERROR: input not found: $input" >&2; exit 1; }
            [ -n "$output" ] || { echo "ERROR: -o is required" >&2; exit 2; }
            echo transcript > "$output"
            """
        )
    }

    try await body(fixture)
}

private func installExecutable(at url: URL, contents: String) throws {
    try contents.write(to: url, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes(
        [.posixPermissions: 0o755],
        ofItemAtPath: url.path
    )
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
    // One last check so a condition that became true during the final
    // sleep is not reported as a timeout.
    return condition()
}
