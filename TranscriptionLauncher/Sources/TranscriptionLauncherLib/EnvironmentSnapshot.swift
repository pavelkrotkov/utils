import Foundation
import Darwin

public enum EnvironmentSnapshot {
    public typealias Values = [String: String]

    private static let cache = EnvironmentSnapshotCache()

    public static func parse(_ rawOutput: String) -> Values {
        var values: Values = [:]
        let separator: Character = rawOutput.contains("\0") ? "\0" : "\n"

        for line in rawOutput.split(separator: separator, omittingEmptySubsequences: false) {
            guard let separatorIndex = line.firstIndex(of: "=") else {
                continue
            }

            let key = String(line[..<separatorIndex])
            let valueStart = line.index(after: separatorIndex)
            let value = String(line[valueStart...])

            guard !key.isEmpty else {
                continue
            }

            if key.hasPrefix("BASH_FUNC_") && value.hasPrefix("() {") {
                continue
            }

            values[key] = value
        }

        return values
    }

    public static func capture() async throws -> Values {
        try await cache.values {
            try await captureUncached()
        }
    }

    @discardableResult
    public static func refresh() async throws -> Values {
        try await cache.refresh {
            try await captureUncached()
        }
    }

    private static func captureUncached() async throws -> Values {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try captureUncachedBlocking())
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private static func captureUncachedBlocking() throws -> Values {
        let process = Process()
        let shellPath = FileManager.default.isExecutableFile(atPath: "/bin/zsh")
            ? "/bin/zsh"
            : "/bin/sh"
        process.executableURL = URL(fileURLWithPath: shellPath)
        process.arguments = shellPath == "/bin/zsh"
            ? ["-l", "-i", "-c", "env -0"]
            : ["-l", "-c", "env -0"]

        let fileManager = FileManager.default
        let outputURL = fileManager.temporaryDirectory.appendingPathComponent(
            "EnvironmentSnapshot-\(UUID().uuidString).out"
        )
        let errorURL = fileManager.temporaryDirectory.appendingPathComponent(
            "EnvironmentSnapshot-\(UUID().uuidString).err"
        )
        defer {
            try? fileManager.removeItem(at: outputURL)
            try? fileManager.removeItem(at: errorURL)
        }
        let temporaryFileAttributes: [FileAttributeKey: Any] = [
            .posixPermissions: 0o600,
        ]

        guard fileManager.createFile(
            atPath: outputURL.path,
            contents: nil,
            attributes: temporaryFileAttributes
        ) else {
            throw EnvironmentSnapshotError.temporaryFileFailed(path: outputURL.path)
        }

        guard fileManager.createFile(
            atPath: errorURL.path,
            contents: nil,
            attributes: temporaryFileAttributes
        ) else {
            throw EnvironmentSnapshotError.temporaryFileFailed(path: errorURL.path)
        }

        let openedOutputHandle = try FileHandle(forWritingTo: outputURL)
        var outputHandle: FileHandle? = openedOutputHandle
        defer {
            try? outputHandle?.close()
        }

        let openedErrorHandle = try FileHandle(forWritingTo: errorURL)
        var errorHandle: FileHandle? = openedErrorHandle
        defer {
            try? errorHandle?.close()
        }

        process.standardOutput = openedOutputHandle
        process.standardError = openedErrorHandle

        try process.run()
        try waitUntilExit(process, timeoutSeconds: 10)

        try outputHandle?.close()
        outputHandle = nil
        try errorHandle?.close()
        errorHandle = nil

        let outputData = try Data(contentsOf: outputURL)
        let errorData = try Data(contentsOf: errorURL)

        guard process.terminationStatus == 0 else {
            let message = String(data: errorData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw EnvironmentSnapshotError.captureFailed(
                status: process.terminationStatus,
                stderr: message ?? ""
            )
        }

        guard let output = String(data: outputData, encoding: .utf8) else {
            throw EnvironmentSnapshotError.invalidUTF8Output
        }

        return parse(output)
    }

    private static func waitUntilExit(_ process: Process, timeoutSeconds: TimeInterval) throws {
        guard !waitForExit(process, timeoutSeconds: timeoutSeconds) else {
            return
        }

        process.terminate()

        if !waitForExit(process, timeoutSeconds: 1), process.isRunning {
            kill(process.processIdentifier, SIGKILL)
            process.waitUntilExit()
        }

        throw EnvironmentSnapshotError.captureTimedOut
    }

    private static func waitForExit(_ process: Process, timeoutSeconds: TimeInterval) -> Bool {
        guard process.isRunning else {
            return true
        }

        let semaphore = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in
            semaphore.signal()
        }

        guard process.isRunning else {
            process.terminationHandler = nil
            return true
        }

        let didExit = semaphore.wait(timeout: .now() + timeoutSeconds) != .timedOut
        process.terminationHandler = nil
        return didExit
    }
}

private actor EnvironmentSnapshotCache {
    private var cachedValues: EnvironmentSnapshot.Values?
    private var inFlightCapture: Task<EnvironmentSnapshot.Values, Error>?
    private var currentCaptureID: UUID?

    func values(
        capture: @Sendable @escaping () async throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        if let cachedValues {
            return cachedValues
        }

        if let inFlightCapture {
            return try await inFlightCapture.value
        }

        return try await runCapture(capture)
    }

    func refresh(
        capture: @Sendable @escaping () async throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        try await runCapture(capture)
    }

    private func runCapture(
        _ capture: @Sendable @escaping () async throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        let task = Task {
            try await capture()
        }
        let captureID = UUID()
        currentCaptureID = captureID
        inFlightCapture = task
        defer {
            if currentCaptureID == captureID {
                inFlightCapture = nil
                currentCaptureID = nil
            }
        }

        let values = try await task.value
        if currentCaptureID == captureID {
            cachedValues = values
        }
        return values
    }
}

public enum EnvironmentSnapshotError: Error, Equatable {
    case captureFailed(status: Int32, stderr: String)
    case captureTimedOut
    case invalidUTF8Output
    case temporaryFileFailed(path: String)
}
