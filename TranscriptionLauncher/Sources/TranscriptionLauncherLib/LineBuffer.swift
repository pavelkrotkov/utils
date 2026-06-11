import Foundation

/// Accumulates raw pipe chunks and splits them into complete `\n`-terminated
/// lines, tolerating chunk boundaries that fall mid-line or mid-character.
public struct LineBuffer {
    private var pending = Data()

    public init() {}

    /// Appends a chunk and returns the lines it completed, without their
    /// trailing newlines. A trailing `\r` is stripped so CRLF output parses
    /// the same as LF output.
    public mutating func append(_ data: Data) -> [String] {
        pending.append(data)

        var lines: [String] = []
        while let newlineIndex = pending.firstIndex(of: Self.newlineByte) {
            lines.append(Self.decode(pending[pending.startIndex..<newlineIndex]))
            pending.removeSubrange(pending.startIndex...newlineIndex)
        }
        return lines
    }

    /// Returns the trailing unterminated line, if any, and empties the buffer.
    public mutating func finish() -> String? {
        guard !pending.isEmpty else {
            return nil
        }

        defer {
            pending = Data()
        }
        return Self.decode(pending)
    }

    private static let newlineByte = UInt8(ascii: "\n")

    private static func decode(_ data: Data) -> String {
        var line = String(decoding: data, as: UTF8.self)
        if line.hasSuffix("\r") {
            line.removeLast()
        }
        return line
    }
}
