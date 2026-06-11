import Testing
import TranscriptionLauncherLib

@Test
func parsesPercentLine() {
    let event = ProgressParser.parse("INFO: whisper-cpp ASR:  45.2%, elapsed 01:23, ETA 01:42")

    #expect(event == ProgressEvent(
        stage: "whisper-cpp ASR",
        percent: 45.2,
        detail: "elapsed 01:23, ETA 01:42"
    ))
}

@Test
func parses100Percent() {
    let event = ProgressParser.parse("INFO: whisper-cpp ASR: 100.0%, elapsed 02:15, ETA unknown")

    #expect(event == ProgressEvent(
        stage: "whisper-cpp ASR",
        percent: 100.0,
        detail: "elapsed 02:15, ETA unknown"
    ))
}

@Test
func parsesStartLine() {
    let event = ProgressParser.parse("INFO: ffmpeg conversion started")

    #expect(event == ProgressEvent(stage: "ffmpeg conversion", isStart: true))
}

@Test
func parsesStartLineWithDetail() {
    let event = ProgressParser.parse("INFO: ffmpeg conversion started (duration 05:30)")

    #expect(event == ProgressEvent(
        stage: "ffmpeg conversion",
        isStart: true,
        detail: "duration 05:30"
    ))
}

@Test
func parsesFinishLine() {
    let event = ProgressParser.parse("INFO: whisper-cpp ASR finished in 02:15")

    #expect(event == ProgressEvent(stage: "whisper-cpp ASR", isFinished: true))
}

@Test
func parsesFinishLineWithDetail() {
    let event = ProgressParser.parse("INFO: whisper-cpp ASR finished in 02:15 (/tmp/whisper.json)")

    #expect(event == ProgressEvent(
        stage: "whisper-cpp ASR",
        isFinished: true,
        detail: "/tmp/whisper.json"
    ))
}

@Test
func parsesInfoMessage() {
    let line = "INFO: Skipping pyannote diarization (default; pass --diarization to enable)"

    let event = ProgressParser.parse(line)

    #expect(event == ProgressEvent(
        stage: "Skipping pyannote diarization (default; pass --diarization to enable)"
    ))
}

@Test
func ignoresNonProgressLine() {
    let event = ProgressParser.parse("Transcribing with OpenAI model: gpt-4o-transcribe...")

    #expect(event == nil)
}

@Test
func parsesStageNameWithSpaces() {
    let event = ProgressParser.parse(
        "INFO: pyannote speaker diarization:  30.0% (3/10), elapsed 00:45, ETA 01:45"
    )

    #expect(event == ProgressEvent(
        stage: "pyannote speaker diarization",
        percent: 30.0,
        detail: "(3/10), elapsed 00:45, ETA 01:45"
    ))
}

@Test
func parsesIndeterminateUpdate() {
    let event = ProgressParser.parse("INFO: openai transcription: processed 01:00, elapsed 02:00")

    #expect(event == ProgressEvent(
        stage: "openai transcription",
        detail: "processed 01:00, elapsed 02:00"
    ))
}

@Test
func parsesLineWithTrailingNewline() {
    let event = ProgressParser.parse("INFO: ffmpeg conversion started\r\n")

    #expect(event == ProgressEvent(stage: "ffmpeg conversion", isStart: true))
}

@Test
func ignoresErrorLine() {
    let event = ProgressParser.parse("ERROR: Whisper model not found: /tmp/model.bin")

    #expect(event == nil)
}
