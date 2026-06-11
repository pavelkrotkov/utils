import Foundation
import Testing
import TranscriptionLauncherLib

@MainActor
@Test
func openURLBufferForwardsDirectlyWhenHandlerIsSet() {
    let buffer = OpenURLBuffer()
    var received: [URL] = []
    buffer.handler = { received.append(contentsOf: $0) }

    buffer.deliver([URL(fileURLWithPath: "/tmp/a.m4a")])

    #expect(received == [URL(fileURLWithPath: "/tmp/a.m4a")])
}

@MainActor
@Test
func openURLBufferFlushesBufferedURLsInOrderWhenHandlerAttaches() {
    let buffer = OpenURLBuffer()
    buffer.deliver([URL(fileURLWithPath: "/tmp/a.m4a")])
    buffer.deliver([URL(fileURLWithPath: "/tmp/b.mp4")])

    var received: [URL] = []
    buffer.handler = { received.append(contentsOf: $0) }

    #expect(received == [
        URL(fileURLWithPath: "/tmp/a.m4a"),
        URL(fileURLWithPath: "/tmp/b.mp4"),
    ])
}

@MainActor
@Test
func openURLBufferDoesNotRedeliverWhenHandlerIsReplaced() {
    let buffer = OpenURLBuffer()
    buffer.deliver([URL(fileURLWithPath: "/tmp/a.m4a")])
    buffer.handler = { _ in }

    var received: [URL] = []
    buffer.handler = { received.append(contentsOf: $0) }

    #expect(received.isEmpty)
}

@MainActor
@Test
func openURLBufferIgnoresEmptyDelivery() {
    let buffer = OpenURLBuffer()
    var callCount = 0
    buffer.handler = { _ in callCount += 1 }

    buffer.deliver([])

    #expect(callCount == 0)
}
