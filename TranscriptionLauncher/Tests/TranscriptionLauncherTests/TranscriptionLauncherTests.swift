import Testing
import TranscriptionLauncherLib

@Test
func appMetadataProvidesDefaultDisplayName() {
    let metadata = AppMetadata()

    #expect(metadata.displayName == "Transcription Launcher")
}
