import Foundation

public struct ProgressEvent: Equatable, Sendable {
    public let stage: String
    /// Completion percentage in 0...100, or nil when indeterminate.
    public let percent: Double?
    public let isStart: Bool
    public let isFinished: Bool
    public let detail: String?

    public init(
        stage: String,
        percent: Double? = nil,
        isStart: Bool = false,
        isFinished: Bool = false,
        detail: String? = nil
    ) {
        self.stage = stage
        self.percent = percent
        self.isStart = isStart
        self.isFinished = isFinished
        self.detail = detail
    }
}

/// Parses the progress lines that ProgressReporter (audio_common.py) writes
/// to stderr:
///
///     INFO: <stage> started
///     INFO: <stage> started (<detail>)
///     INFO: <stage>:  45.2%, elapsed 01:23, ETA 01:42
///     INFO: <stage>: elapsed 01:23
///     INFO: <stage> finished in 02:15
///     INFO: <stage> finished in 02:15 (<detail>)
///     INFO: <free-form message>
public enum ProgressParser {
    public static func parse(_ line: String) -> ProgressEvent? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix(infoPrefix) else {
            return nil
        }

        let message = String(trimmed.dropFirst(infoPrefix.count))
        if let event = parseFinish(message) ?? parseStart(message) ?? parseUpdate(message) {
            return event
        }

        // Free-form reporter.info() message such as
        // "Skipping pyannote diarization (default; pass --diarization to enable)".
        return ProgressEvent(stage: message)
    }

    private static let infoPrefix = "INFO: "

    private static func parseStart(_ message: String) -> ProgressEvent? {
        guard let match = message.wholeMatch(of: /(.+) started(?: \((.+)\))?/) else {
            return nil
        }

        return ProgressEvent(
            stage: String(match.1),
            isStart: true,
            detail: match.2.map(String.init)
        )
    }

    private static func parseFinish(_ message: String) -> ProgressEvent? {
        guard let match = message.wholeMatch(of: /(.+) finished in \S+(?: \((.+)\))?/) else {
            return nil
        }

        return ProgressEvent(
            stage: String(match.1),
            isFinished: true,
            detail: match.2.map(String.init)
        )
    }

    private static func parseUpdate(_ message: String) -> ProgressEvent? {
        guard let match = message.wholeMatch(of: /(.+?): +(.+)/) else {
            return nil
        }

        let stage = String(match.1)
        let rest = String(match.2)

        guard let percentMatch = rest.wholeMatch(of: /(\d+(?:\.\d+)?)%,? *(.*)/) else {
            // Indeterminate update such as "processed 01:00, elapsed 02:00".
            return ProgressEvent(stage: stage, detail: rest)
        }

        let detail = String(percentMatch.2)
        return ProgressEvent(
            stage: stage,
            percent: Double(percentMatch.1),
            detail: detail.isEmpty ? nil : detail
        )
    }
}
