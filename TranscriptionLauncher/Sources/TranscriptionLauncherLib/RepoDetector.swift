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
            .resolvingSymlinksInPath()
            .standardizedFileURL
        var visitedPaths = Set<String>()

        while true {
            let currentPath = currentURL.path(percentEncoded: false)
            guard visitedPaths.insert(currentPath).inserted else {
                return nil
            }

            if isUsableDirectory(currentURL, fileManager: fileManager),
               containsMarkerFile(in: currentURL, markerFileNames: markerFileNames, fileManager: fileManager) {
                return currentURL
            }

            let parentURL = currentURL
                .deletingLastPathComponent()
                .resolvingSymlinksInPath()
                .standardizedFileURL
            if parentURL.path(percentEncoded: false) == currentPath {
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

    private static func isUsableDirectory(_ url: URL, fileManager: FileManager) -> Bool {
        var isDirectory: ObjCBool = false
        let path = url.path(percentEncoded: false)

        return fileManager.fileExists(atPath: path, isDirectory: &isDirectory)
            && isDirectory.boolValue
            && fileManager.isReadableFile(atPath: path)
            && fileManager.isExecutableFile(atPath: path)
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
