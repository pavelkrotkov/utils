import Foundation

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
        try await Task.detached(priority: .userInitiated) {
            try captureUncachedBlocking()
        }.value
    }

    private static func captureUncachedBlocking() throws -> Values {
        let process = Process()
        let shellPath = FileManager.default.isExecutableFile(atPath: "/bin/zsh")
            ? "/bin/zsh"
            : "/bin/sh"
        process.executableURL = URL(fileURLWithPath: shellPath)
        process.arguments = shellPath == "/bin/zsh"
            ? ["-l", "-c", "env -0"]
            : ["-c", "env -0"]

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

        guard fileManager.createFile(atPath: outputURL.path, contents: nil) else {
            throw EnvironmentSnapshotError.temporaryFileFailed(path: outputURL.path)
        }

        guard fileManager.createFile(atPath: errorURL.path, contents: nil) else {
            throw EnvironmentSnapshotError.temporaryFileFailed(path: errorURL.path)
        }

        let outputHandle = try FileHandle(forWritingTo: outputURL)
        let errorHandle = try FileHandle(forWritingTo: errorURL)
        defer {
            try? outputHandle.close()
            try? errorHandle.close()
        }

        process.standardOutput = outputHandle
        process.standardError = errorHandle

        try process.run()
        process.waitUntilExit()

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
}

private actor EnvironmentSnapshotCache {
    private var cachedValues: EnvironmentSnapshot.Values?
    private var inFlightCapture: Task<EnvironmentSnapshot.Values, Error>?

    func values(
        capture: @Sendable @escaping () async throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        if let cachedValues {
            return cachedValues
        }

        if let inFlightCapture {
            return try await inFlightCapture.value
        }

        let task = Task {
            try await capture()
        }
        inFlightCapture = task
        defer {
            inFlightCapture = nil
        }

        let values = try await task.value
        cachedValues = values
        return values
    }

    func refresh(
        capture: @Sendable @escaping () async throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        let task = Task {
            try await capture()
        }
        inFlightCapture = task
        defer {
            inFlightCapture = nil
        }

        let values = try await task.value
        cachedValues = values
        return values
    }
}

public enum EnvironmentSnapshotError: Error, Equatable {
    case captureFailed(status: Int32, stderr: String)
    case invalidUTF8Output
    case temporaryFileFailed(path: String)
}
