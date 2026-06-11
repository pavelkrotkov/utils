import Foundation

public enum TranscriptionError: Equatable, Sendable {
    case missingAPIKey(String)
    case missingDependency(String)
    case missingModel(String)
    case unsupportedHardware(String)
    case apiError(String)
    case unknown(String)
}

public enum ErrorClassifier {
    public static func classify(_ stderr: String) -> TranscriptionError {
        let lines = stderr
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }

        // Matchers are ordered from most to least specific. Every line is
        // checked against a matcher before moving to the next one, so the
        // most specific classification wins regardless of line order.
        for matcher in matchers {
            for line in lines {
                if let error = matcher(line) {
                    return error
                }
            }
        }

        return .unknown(stderr.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private static let matchers: [@Sendable (String) -> TranscriptionError?] = [
        matchMissingOpenAIKey,
        matchMissingHFToken,
        matchMissingModel,
        matchMissingBinary,
        matchCommandNotFound,
        matchMissingPythonPackage,
        matchUnsupportedHardware,
        matchAPIError,
    ]

    private static func matchMissingOpenAIKey(_ line: String) -> TranscriptionError? {
        guard line.contains("OPENAI_API_KEY is not set") else {
            return nil
        }
        return .missingAPIKey("OPENAI_API_KEY")
    }

    private static func matchMissingHFToken(_ line: String) -> TranscriptionError? {
        guard line.contains("Failed to load both pyannote/speaker-diarization") else {
            return nil
        }
        return .missingAPIKey("HF_TOKEN")
    }

    private static func matchMissingModel(_ line: String) -> TranscriptionError? {
        guard let path = value(after: "Whisper model not found:", in: line) else {
            return nil
        }
        return .missingModel(path)
    }

    private static func matchMissingBinary(_ line: String) -> TranscriptionError? {
        guard let binary = value(after: "binary not found or not executable:", in: line) else {
            return nil
        }

        let name = binary.split(separator: "/").last.map(String.init) ?? binary
        if name.hasPrefix("whisper") {
            return .missingDependency("whisper-cpp")
        }
        return .missingDependency(name)
    }

    private static func matchCommandNotFound(_ line: String) -> TranscriptionError? {
        // zsh reports "zsh: command not found: uv".
        if let name = value(after: "command not found:", in: line) {
            return .missingDependency(name)
        }

        // bash reports "bash: line 1: uv: command not found".
        if let suffixRange = line.range(of: ": command not found") {
            let prefix = line[..<suffixRange.lowerBound]
            let name = prefix.components(separatedBy: ": ").last ?? String(prefix)
            let trimmed = name.trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty {
                return .missingDependency(trimmed)
            }
        }

        return nil
    }

    private static func matchMissingPythonPackage(_ line: String) -> TranscriptionError? {
        guard let detail = value(after: "Missing required Python package:", in: line) else {
            return nil
        }

        // The detail is an ImportError description such as
        // "No module named 'mlx_audio'"; fall back to the raw text otherwise.
        var name = detail
        if let openQuote = detail.firstIndex(of: "'") {
            let afterQuote = detail.index(after: openQuote)
            if let closeQuote = detail[afterQuote...].firstIndex(of: "'") {
                name = String(detail[afterQuote..<closeQuote])
            }
        }

        return .missingDependency(name.replacingOccurrences(of: "_", with: "-"))
    }

    private static func matchUnsupportedHardware(_ line: String) -> TranscriptionError? {
        guard line.contains("intended for Apple Silicon Macs") else {
            return nil
        }
        return .unsupportedHardware("VibeVoice requires Apple Silicon")
    }

    private static func matchAPIError(_ line: String) -> TranscriptionError? {
        guard line.contains("OpenAI API request failed (HTTP") else {
            return nil
        }

        var message = line
        for prefix in ["Error: ", "ERROR: "] where message.hasPrefix(prefix) {
            message = String(message.dropFirst(prefix.count))
        }
        return .apiError(message)
    }

    /// Returns the trimmed remainder of `line` after `marker`, or nil when the
    /// marker is absent or nothing follows it.
    private static func value(after marker: String, in line: String) -> String? {
        guard let range = line.range(of: marker) else {
            return nil
        }

        let remainder = line[range.upperBound...].trimmingCharacters(in: .whitespaces)
        return remainder.isEmpty ? nil : remainder
    }
}
