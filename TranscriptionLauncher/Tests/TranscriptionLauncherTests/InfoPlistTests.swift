import Foundation
import Testing

/// The package Info.plist (consumed by app bundle packaging, #66) must keep
/// the app registered for Finder's "Open With" menu on audio and video files.
@Test
func infoPlistDeclaresAudioAndMovieDocumentTypes() throws {
    let plistURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()  // TranscriptionLauncherTests
        .deletingLastPathComponent()  // Tests
        .deletingLastPathComponent()  // TranscriptionLauncher
        .appendingPathComponent("Info.plist")
    let data = try Data(contentsOf: plistURL)
    let plist = try #require(
        try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
    )

    let documentTypes = try #require(plist["CFBundleDocumentTypes"] as? [[String: Any]])
    let contentTypes = documentTypes.flatMap { $0["LSItemContentTypes"] as? [String] ?? [] }

    #expect(contentTypes.contains("public.audio"))
    #expect(contentTypes.contains("public.movie"))
    for documentType in documentTypes {
        #expect(documentType["CFBundleTypeRole"] as? String == "Viewer")
    }
}
