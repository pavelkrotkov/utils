import Combine
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
            environment: environment,
            workingDirectory: command.workingDirectory
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

        // Bound the wait for end-of-file: orphaned grandchildren can hold
        // the pipe write ends open indefinitely. `forceFinish` is
        // idempotent, so firing after a natural finish is harmless.
        let drainTimeout = Task { @MainActor in
            try? await Task.sleep(for: Self.drainGracePeriod)
            forceFinish()
        }
        await pump.value
        drainTimeout.cancel()

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

    /// Requests termination of the running process tree; `run` then throws
    /// `CancellationError`. SIGTERM is escalated to SIGKILL when the
    /// process has not exited after a grace period.
    ///
    /// `Process` does not make the child a process-group leader, so a
    /// wrapper like a shell or `uv` that does not forward signals would
    /// leave the real worker running. The descendant tree is therefore
    /// snapshotted and signalled directly — best effort: workers spawned
    /// between the snapshot and the signal are caught only when the
    /// wrapper forwards or leads its own group.
    public func cancel() {
        guard isRunning, let process, process.isRunning else {
            return
        }

        isCancelled = true
        let pid = process.processIdentifier
        // Snapshot before signalling: once the wrapper dies, orphaned
        // descendants reparent to launchd and can no longer be discovered
        // from the tree.
        let descendants = Self.descendantPIDs(of: pid)
        Self.signalTree(rootPID: pid, descendants: descendants, signal: SIGTERM)

        Task { @MainActor [weak self] in
            try? await Task.sleep(for: Self.killGracePeriod)
            if let self,
               let current = self.process,
               current.processIdentifier == pid,
               current.isRunning {
                Self.signalTree(rootPID: pid, descendants: [], signal: SIGKILL)
            }
            for survivor in descendants where kill(survivor, 0) == 0 {
                kill(survivor, SIGKILL)
            }
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

    /// Signals the wrapper — through its whole process group when it is
    /// the leader, which also covers children spawned after the descendant
    /// snapshot — and then each snapshotted descendant individually.
    private nonisolated static func signalTree(
        rootPID: pid_t,
        descendants: [pid_t],
        signal: Int32
    ) {
        if getpgid(rootPID) == rootPID {
            kill(-rootPID, signal)
        } else {
            kill(rootPID, signal)
        }

        for pid in descendants {
            kill(pid, signal)
        }
    }

    /// Returns the PIDs of all live descendants of `rootPID`, discovered
    /// by walking the parent links of the full process table.
    private nonisolated static func descendantPIDs(of rootPID: pid_t) -> [pid_t] {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
        var size = 0
        guard sysctl(&mib, UInt32(mib.count), nil, &size, nil, 0) == 0, size > 0 else {
            return []
        }

        // Headroom for processes spawned between the two sysctl calls.
        var processes = [kinfo_proc](
            repeating: kinfo_proc(),
            count: size / MemoryLayout<kinfo_proc>.stride + 16
        )
        size = processes.count * MemoryLayout<kinfo_proc>.stride
        guard sysctl(&mib, UInt32(mib.count), &processes, &size, nil, 0) == 0 else {
            return []
        }

        var childrenByParent: [pid_t: [pid_t]] = [:]
        for info in processes.prefix(size / MemoryLayout<kinfo_proc>.stride) {
            childrenByParent[info.kp_eproc.e_ppid, default: []].append(info.kp_proc.p_pid)
        }

        var descendants: [pid_t] = []
        var queue = [rootPID]
        while let parent = queue.popLast() {
            for child in childrenByParent[parent] ?? [] {
                descendants.append(child)
                queue.append(child)
            }
        }
        return descendants
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
