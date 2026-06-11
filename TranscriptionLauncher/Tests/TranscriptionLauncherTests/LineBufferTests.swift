import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func splitsCompleteLines() {
    var buffer = LineBuffer()

    let lines = buffer.append(Data("one\ntwo\n".utf8))

    #expect(lines == ["one", "two"])
    #expect(buffer.finish() == nil)
}

@Test
func buffersPartialLineAcrossChunks() {
    var buffer = LineBuffer()

    #expect(buffer.append(Data("par".utf8)) == [])
    #expect(buffer.append(Data("tial\nnext".utf8)) == ["partial"])
    #expect(buffer.finish() == "next")
    #expect(buffer.finish() == nil)
}

@Test
func stripsCarriageReturns() {
    var buffer = LineBuffer()

    #expect(buffer.append(Data("crlf\r\nplain\n".utf8)) == ["crlf", "plain"])
}

@Test
func handlesMultibyteCharacterSplitAcrossChunks() {
    var buffer = LineBuffer()
    let bytes = Array("héllo\n".utf8)

    #expect(buffer.append(Data(bytes[..<2])) == [])
    #expect(buffer.append(Data(bytes[2...])) == ["héllo"])
}

@Test
func preservesEmptyLines() {
    var buffer = LineBuffer()

    #expect(buffer.append(Data("\n\n".utf8)) == ["", ""])
}
