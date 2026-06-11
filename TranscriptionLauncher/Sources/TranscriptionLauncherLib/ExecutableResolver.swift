import Foundation

public enum ExecutableResolver {
    /// Resolves a command's executable to an absolute file URL. Relative
    /// values containing a path separator are resolved against
    /// `workingDirectory` (matching where the child process would exec
    /// them), falling back to the host's current directory; bare names
    /// (e.g. `uv`) are searched in the given environment's `PATH`,
    /// mirroring shell lookup. Returns nil when no executable regular file
    /// is found.
    public static func resolve(
        _ executable: String,
        environment: [String: String],
        workingDirectory: URL? = nil
    ) -> URL? {
        guard !executable.isEmpty else {
            return nil
        }

        if executable.contains("/") {
            let candidate: URL
            if let workingDirectory, !executable.hasPrefix("/") {
                candidate = URL(
                    fileURLWithPath: executable,
                    isDirectory: false,
                    relativeTo: workingDirectory
                )
            } else {
                candidate = URL(fileURLWithPath: executable, isDirectory: false)
            }

            guard isExecutableRegularFile(atPath: candidate.path) else {
                return nil
            }
            return candidate.absoluteURL
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
