import Testing
import TranscriptionLauncherLib

@Test
func appMetadataProvidesDefaultDisplayName() {
    let metadata = AppMetadata()

    #expect(metadata.displayName == "Transcription Launcher")
}

@Test
func environmentSnapshotParsesBasicOutput() {
    let values = EnvironmentSnapshot.parse("HOME=/Users/me\nPATH=/usr/bin")

    #expect(values == [
        "HOME": "/Users/me",
        "PATH": "/usr/bin",
    ])
}

@Test
func environmentSnapshotParsesValueContainingEquals() {
    let values = EnvironmentSnapshot.parse("PROMPT=foo=bar=baz")

    #expect(values["PROMPT"] == "foo=bar=baz")
}

@Test
func environmentSnapshotParsesEmptyValue() {
    let values = EnvironmentSnapshot.parse("EMPTY_VAR=")

    #expect(values["EMPTY_VAR"] == "")
}

@Test
func environmentSnapshotSkipsMalformedLines() {
    let values = EnvironmentSnapshot.parse("GOOD=value\nno_equals_here\nALSO_GOOD=123")

    #expect(values == [
        "GOOD": "value",
        "ALSO_GOOD": "123",
    ])
}

@Test
func environmentSnapshotSkipsShellFunctionExports() {
    let values = EnvironmentSnapshot.parse(
        """
        GOOD=value
        BASH_FUNC_foo%%=() {
        ALSO_GOOD=123
        """
    )

    #expect(values == [
        "GOOD": "value",
        "ALSO_GOOD": "123",
    ])
}
