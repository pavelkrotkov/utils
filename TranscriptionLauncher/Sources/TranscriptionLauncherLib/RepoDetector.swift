import Foundation

public enum RepoDetector {
    public static let defaultMarkerFileNames = [
        "audio_transcribe_openai.sh",
        "audio_common.py",
        "pyproject.toml",
    ]

    public static func findRepoRoot(
        startingFrom startURL: URL,
        markerFileNames: [String] = defaultMarkerFileNames,
        fileManager: FileManager = .default
    ) -> URL? {
        guard !markerFileNames.isEmpty else {
            return nil
        }

        var currentURL = directoryURL(for: startURL, fileManager: fileManager)

        while true {
            if containsMarkerFile(in: currentURL, markerFileNames: markerFileNames, fileManager: fileManager) {
                return currentURL
            }

            let parentURL = currentURL.deletingLastPathComponent().standardizedFileURL
            if parentURL.path == currentURL.path {
                return nil
            }

            currentURL = parentURL
        }
    }

    private static func directoryURL(for url: URL, fileManager: FileManager) -> URL {
        let standardizedURL = url.standardizedFileURL
        var isDirectory: ObjCBool = false

        if fileManager.fileExists(atPath: standardizedURL.path(percentEncoded: false), isDirectory: &isDirectory),
           !isDirectory.boolValue {
            return standardizedURL.deletingLastPathComponent().standardizedFileURL
        }

        return standardizedURL
    }

    private static func containsMarkerFile(
        in directoryURL: URL,
        markerFileNames: [String],
        fileManager: FileManager
    ) -> Bool {
        markerFileNames.contains { markerFileName in
            let markerURL = directoryURL.appendingPathComponent(markerFileName)
            var isDirectory: ObjCBool = false
            return fileManager.fileExists(atPath: markerURL.path(percentEncoded: false), isDirectory: &isDirectory)
                && !isDirectory.boolValue
        }
    }
}
