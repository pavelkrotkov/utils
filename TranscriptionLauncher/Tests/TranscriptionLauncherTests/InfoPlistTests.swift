import Foundation
import Testing

/// The Info.plist that Scripts/make-app.sh ships in the .app bundle must
/// keep the app registered for Finder's "Open With" menu on audio and
/// video files.
@Test
func infoPlistDeclaresAudioAndMovieDocumentTypes() throws {
    let plistURL = URL(filePath: #filePath)
        .deletingLastPathComponent()  // TranscriptionLauncherTests
        .deletingLastPathComponent()  // Tests
        .deletingLastPathComponent()  // TranscriptionLauncher
        .appendingPathComponent("Packaging")
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
