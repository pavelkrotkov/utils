import Foundation

public enum EnvironmentSnapshot {
    public typealias Values = [String: String]

    private static let cache = EnvironmentSnapshotCache()

    public static func parse(_ rawOutput: String) -> Values {
        var values: Values = [:]

        for line in rawOutput.split(separator: "\n", omittingEmptySubsequences: false) {
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
            try captureUncached()
        }
    }

    @discardableResult
    public static func refresh() async throws -> Values {
        try await cache.refresh {
            try captureUncached()
        }
    }

    private static func captureUncached() throws -> Values {
        let process = Process()
        let shellPath = FileManager.default.isExecutableFile(atPath: "/bin/zsh")
            ? "/bin/zsh"
            : "/bin/sh"
        process.executableURL = URL(fileURLWithPath: shellPath)
        process.arguments = shellPath == "/bin/zsh"
            ? ["-l", "-c", "env"]
            : ["-c", "env"]

        let outputPipe = Pipe()
        let errorPipe = Pipe()
        process.standardOutput = outputPipe
        process.standardError = errorPipe

        let outputHandle = outputPipe.fileHandleForReading
        let errorHandle = errorPipe.fileHandleForReading
        defer {
            try? outputHandle.close()
            try? errorHandle.close()
        }

        try process.run()
        process.waitUntilExit()

        let outputData = outputHandle.readDataToEndOfFile()
        let errorData = errorHandle.readDataToEndOfFile()

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

    func values(
        capture: @Sendable () throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        if let cachedValues {
            return cachedValues
        }

        let values = try capture()
        cachedValues = values
        return values
    }

    func refresh(
        capture: @Sendable () throws -> EnvironmentSnapshot.Values
    ) async throws -> EnvironmentSnapshot.Values {
        let values = try capture()
        cachedValues = values
        return values
    }
}

public enum EnvironmentSnapshotError: Error, Equatable {
    case captureFailed(status: Int32, stderr: String)
    case invalidUTF8Output
}
