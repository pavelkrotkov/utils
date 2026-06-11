import Darwin
import Foundation

public enum ProcessRunnerError: Error, Equatable {
    case alreadyRunning
    case outputFileMissing(URL)
}

/// Runs a `TranscriptionCommand` as a child process: streams stdout/stderr
/// lines into `logLines`, derives `progress` from stderr via
/// `ProgressParser`, classifies failures with `ErrorClassifier`, and
/// supports cancellation that reaches grandchildren such as the Python
/// process under `uv`.
@MainActor
public final class ProcessRunner: ObservableObject {
    @Published public private(set) var isRunning = false
    @Published public private(set) var progress: ProgressEvent?
    @Published public private(set) var logLines: [String] = []

    /// How long `cancel` waits for SIGTERM to work before sending SIGKILL.
    private static let killGracePeriod = Duration.seconds(2)
    /// How long to wait after exit for the pipes to reach end-of-file.
    /// Orphaned grandchildren can inherit the write ends and never close
    /// them, so the wait must be bounded.
    private static let drainGracePeriod = Duration.seconds(2)

    private var process: Process?
    private var isCancelled = false
    private var pumpFinished = false
    private var stderrTranscript: [String] = []

    public init() {}

    /// Runs the command to completion and returns the produced output file.
    ///
    /// `environment` becomes the child's entire environment (typically a
    /// cached `EnvironmentSnapshot`); its `PATH` also resolves bare
    /// executable names such as `uv`. `onLine` receives every stdout and
    /// stderr line on the main actor as it arrives, in addition to
    /// `logLines`.
    @discardableResult
    public func run(
        command: TranscriptionCommand,
        environment: [String: String],
        onLine: (@MainActor (String) -> Void)? = nil
    ) async throws -> URL {
        guard !isRunning else {
            throw ProcessRunnerError.alreadyRunning
        }

        guard let executableURL = ExecutableResolver.resolve(
            command.executable,
            environment: environment
        ) else {
            throw TranscriptionError.missingDependency(
                URL(fileURLWithPath: command.executable).lastPathComponent
            )
        }

        let process = Process()
        process.executableURL = executableURL
        process.arguments = command.arguments
        process.environment = environment
        process.currentDirectoryURL = command.workingDirectory
        process.standardInput = FileHandle.nullDevice

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        let exitLatch = ExitLatch()
        // Explicitly @Sendable: Foundation invokes the handler on a
        // background queue, so the closure must not inherit this method's
        // main-actor isolation or the runtime isolation check traps.
        process.terminationHandler = { @Sendable _ in
            exitLatch.signal()
        }

        isCancelled = false
        pumpFinished = false
        progress = nil
        logLines = []
        stderrTranscript = []

        try process.run()

        self.process = process
        isRunning = true
        defer {
            self.process = nil
            isRunning = false
        }

        let (lines, forceFinish) = Self.lineStream(
            stdout: stdoutPipe.fileHandleForReading,
            stderr: stderrPipe.fileHandleForReading
        )

        let pump = Task { @MainActor in
            for await line in lines {
                self.consume(line: line, onLine: onLine)
            }
            self.pumpFinished = true
        }

        // The latch ignores task cancellation: once launched, the process
        // must be waited on. Cancellation of the surrounding task is mapped
        // to `cancel()` so the wait still ends promptly.
        await withTaskCancellationHandler {
            await exitLatch.wait()
        } onCancel: {
            Task { @MainActor in
                self.cancel()
            }
        }

        let drainDeadline = ContinuousClock.now + Self.drainGracePeriod
        while !pumpFinished, ContinuousClock.now < drainDeadline {
            try? await Task.sleep(for: .milliseconds(20))
        }
        forceFinish()
        await pump.value

        if isCancelled || Task.isCancelled {
            throw CancellationError()
        }

        guard process.terminationReason == .exit, process.terminationStatus == 0 else {
            throw Self.failureError(
                stderr: stderrTranscript.joined(separator: "\n"),
                reason: process.terminationReason,
                status: process.terminationStatus
            )
        }

        guard FileManager.default.fileExists(atPath: command.outputFile.path) else {
            throw ProcessRunnerError.outputFileMissing(command.outputFile)
        }

        return command.outputFile
    }

    /// Requests termination of the running process; `run` then throws
    /// `CancellationError`. SIGTERM is escalated to SIGKILL when the
    /// process has not exited after a grace period.
    public func cancel() {
        guard isRunning, let process, process.isRunning else {
            return
        }

        isCancelled = true
        let pid = process.processIdentifier
        Self.sendSignal(SIGTERM, to: pid)

        Task { @MainActor [weak self] in
            try? await Task.sleep(for: Self.killGracePeriod)
            guard let self,
                  let current = self.process,
                  current.processIdentifier == pid,
                  current.isRunning else {
                return
            }
            Self.sendSignal(SIGKILL, to: pid)
        }
    }

    private func consume(line: OutputLine, onLine: (@MainActor (String) -> Void)?) {
        logLines.append(line.text)
        onLine?(line.text)

        guard line.isStderr else {
            return
        }

        stderrTranscript.append(line.text)
        if let event = ProgressParser.parse(line.text) {
            progress = event
        }
    }

    /// Signals the whole process group when the child is its leader, so the
    /// signal reaches grandchildren directly; otherwise signals just the
    /// child and relies on parents like `uv` forwarding it.
    private nonisolated static func sendSignal(_ signal: Int32, to pid: pid_t) {
        if getpgid(pid) == pid {
            kill(-pid, signal)
        } else {
            kill(pid, signal)
        }
    }

    private nonisolated static func failureError(
        stderr: String,
        reason: Process.TerminationReason,
        status: Int32
    ) -> TranscriptionError {
        guard stderr.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return ErrorClassifier.classify(stderr)
        }

        let description = reason == .uncaughtSignal
            ? "Process terminated by signal \(status)"
            : "Process exited with status \(status)"
        return .unknown(description)
    }

    /// Returns a stream of output lines from both pipes, finishing when
    /// both reach end-of-file, plus an idempotent `forceFinish` that stops
    /// reading early when a lingering grandchild holds a pipe open.
    private nonisolated static func lineStream(
        stdout: FileHandle,
        stderr: FileHandle
    ) -> (lines: AsyncStream<OutputLine>, forceFinish: () -> Void) {
        let (stream, continuation) = AsyncStream.makeStream(of: OutputLine.self)
        let tracker = StreamTracker(continuation: continuation)
        readLines(from: stdout, isStderr: false, tracker: tracker, into: continuation)
        readLines(from: stderr, isStderr: true, tracker: tracker, into: continuation)

        return (stream, {
            stdout.readabilityHandler = nil
            stderr.readabilityHandler = nil
            tracker.forceFinish()
        })
    }

    private nonisolated static func readLines(
        from handle: FileHandle,
        isStderr: Bool,
        tracker: StreamTracker,
        into continuation: AsyncStream<OutputLine>.Continuation
    ) {
        let accumulator = LineAccumulator()
        // Explicitly @Sendable for the same reason as terminationHandler:
        // the handler runs on a background queue.
        handle.readabilityHandler = { @Sendable handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                if let line = accumulator.buffer.finish() {
                    continuation.yield(OutputLine(text: line, isStderr: isStderr))
                }
                handle.readabilityHandler = nil
                tracker.streamReachedEndOfFile()
                return
            }

            for line in accumulator.buffer.append(data) {
                continuation.yield(OutputLine(text: line, isStderr: isStderr))
            }
        }
    }
}

private struct OutputLine: Sendable {
    let text: String
    let isStderr: Bool
}

/// Only mutated from its handle's readability handler, which Foundation
/// invokes serially per handle.
private final class LineAccumulator: @unchecked Sendable {
    var buffer = LineBuffer()
}

/// Finishes the line-stream continuation once both pipes reach end-of-file,
/// or immediately on `forceFinish`. A lock-guarded counter is used instead
/// of `DispatchGroup` because `forceFinish` would leave a group permanently
/// unbalanced, which is an error to deallocate.
private final class StreamTracker: @unchecked Sendable {
    private let lock = NSLock()
    private var remainingStreams = 2
    private var isFinished = false
    private let continuation: AsyncStream<OutputLine>.Continuation

    init(continuation: AsyncStream<OutputLine>.Continuation) {
        self.continuation = continuation
    }

    func streamReachedEndOfFile() {
        finish(force: false)
    }

    func forceFinish() {
        finish(force: true)
    }

    private func finish(force: Bool) {
        lock.lock()
        if !force {
            remainingStreams -= 1
        }
        let shouldFinish = !isFinished && (force || remainingStreams == 0)
        if shouldFinish {
            isFinished = true
        }
        lock.unlock()

        if shouldFinish {
            continuation.finish()
        }
    }
}

/// A one-shot latch that, unlike `AsyncStream` iteration, is immune to task
/// cancellation: `wait()` always suspends until `signal()`.
private final class ExitLatch: @unchecked Sendable {
    private let lock = NSLock()
    private var isSignalled = false
    private var continuation: CheckedContinuation<Void, Never>?

    func signal() {
        lock.lock()
        isSignalled = true
        let continuation = self.continuation
        self.continuation = nil
        lock.unlock()

        continuation?.resume()
    }

    func wait() async {
        await withCheckedContinuation { continuation in
            lock.lock()
            if isSignalled {
                lock.unlock()
                continuation.resume()
                return
            }
            self.continuation = continuation
            lock.unlock()
        }
    }
}
