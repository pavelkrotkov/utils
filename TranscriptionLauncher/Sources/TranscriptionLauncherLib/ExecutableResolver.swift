import Foundation

public enum ExecutableResolver {
    /// Resolves a command's executable to an absolute file URL. Values
    /// containing a path separator are used as-is; bare names (e.g. `uv`)
    /// are searched in the given environment's `PATH`, mirroring shell
    /// lookup. Returns nil when no executable regular file is found.
    public static func resolve(_ executable: String, environment: [String: String]) -> URL? {
        guard !executable.isEmpty else {
            return nil
        }

        if executable.contains("/") {
            guard isExecutableRegularFile(atPath: executable) else {
                return nil
            }
            return URL(fileURLWithPath: executable, isDirectory: false)
        }

        for directory in (environment["PATH"] ?? "").split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory), isDirectory: true)
                .appendingPathComponent(executable, isDirectory: false)
            if isExecutableRegularFile(atPath: candidate.path) {
                return candidate
            }
        }

        return nil
    }

    /// Directories pass `isExecutableFile`, so reject them explicitly.
    private static func isExecutableRegularFile(atPath path: String) -> Bool {
        let fileManager = FileManager.default
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: path, isDirectory: &isDirectory),
              !isDirectory.boolValue else {
            return false
        }
        return fileManager.isExecutableFile(atPath: path)
    }
}
