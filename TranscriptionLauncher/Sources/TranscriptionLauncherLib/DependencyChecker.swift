import Foundation

/// Checks whether the external tools and credentials the transcription
/// presets rely on are available in a captured environment.
public enum DependencyChecker {
    public enum Requirement: Equatable, Sendable {
        case localPresets
        case cloudPresets
    }

    public struct Item: Equatable, Sendable {
        public let name: String
        public let requirement: Requirement
        public let isAvailable: Bool
        /// Absolute path of the resolved executable; nil for environment
        /// variables and for executables that were not found.
        public let resolvedPath: String?

        public init(
            name: String,
            requirement: Requirement,
            isAvailable: Bool,
            resolvedPath: String? = nil
        ) {
            self.name = name
            self.requirement = requirement
            self.isAvailable = isAvailable
            self.resolvedPath = resolvedPath
        }
    }

    /// Executables every local preset needs on `PATH`.
    public static let localExecutableNames = ["ffmpeg", "uv"]

    /// Environment variables the cloud presets need.
    public static let cloudVariableNames = ["OPENAI_API_KEY"]

    /// Availability is advisory: a missing entry means the matching presets
    /// won't run, not that the app is unusable.
    public static func check(environment: [String: String]) -> [Item] {
        var items: [Item] = []

        for name in localExecutableNames {
            let resolvedURL = ExecutableResolver.resolve(name, environment: environment)
            items.append(Item(
                name: name,
                requirement: .localPresets,
                isAvailable: resolvedURL != nil,
                resolvedPath: resolvedURL?.path(percentEncoded: false)
            ))
        }

        for name in cloudVariableNames {
            let value = environment[name]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            items.append(Item(
                name: name,
                requirement: .cloudPresets,
                isAvailable: !value.isEmpty
            ))
        }

        return items
    }
}
