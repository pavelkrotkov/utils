import Testing
import TranscriptionLauncherLib

@Test
func classifierDetectsMissingOpenAIKey() {
    let error = ErrorClassifier.classify("Error: OPENAI_API_KEY is not set.")

    #expect(error == .missingAPIKey("OPENAI_API_KEY"))
}

@Test
func classifierDetectsMissingHFToken() {
    let stderr = """
    ERROR: Failed to load both pyannote/speaker-diarization-3.1 \
    and pyannote/speaker-diarization-community-1: 401 Client Error
    """

    #expect(ErrorClassifier.classify(stderr) == .missingAPIKey("HF_TOKEN"))
}

@Test
func classifierDetectsMissingWhisperModel() {
    let stderr = "ERROR: Whisper model not found: /opt/models/ggml-large-v3.bin"

    #expect(ErrorClassifier.classify(stderr) == .missingModel("/opt/models/ggml-large-v3.bin"))
}

@Test
func classifierDetectsMissingUv() {
    #expect(ErrorClassifier.classify("zsh: command not found: uv") == .missingDependency("uv"))
}

@Test
func classifierDetectsMissingUvFromBashStyleMessage() {
    let stderr = "bash: line 1: uv: command not found"

    #expect(ErrorClassifier.classify(stderr) == .missingDependency("uv"))
}

@Test
func classifierDetectsMissingFfmpeg() {
    let stderr = "ERROR: ffmpeg binary not found or not executable: ffmpeg"

    #expect(ErrorClassifier.classify(stderr) == .missingDependency("ffmpeg"))
}

@Test
func classifierDetectsMissingWhisperBinary() {
    let stderr = "ERROR: whisper binary not found or not executable: whisper"

    #expect(ErrorClassifier.classify(stderr) == .missingDependency("whisper-cpp"))
}

@Test
func classifierNormalizesWhisperCliBinaryPath() {
    let stderr = "ERROR: whisper binary not found or not executable: /opt/homebrew/bin/whisper-cli"

    #expect(ErrorClassifier.classify(stderr) == .missingDependency("whisper-cpp"))
}

@Test
func classifierDetectsMissingMlxAudio() {
    let stderr = "ERROR: Missing required Python package: No module named 'mlx_audio'"

    #expect(ErrorClassifier.classify(stderr) == .missingDependency("mlx-audio"))
}

@Test
func classifierDetectsUnsupportedHardware() {
    let stderr = "ERROR: mlx-audio/VibeVoice-ASR is intended for Apple Silicon Macs (Darwin arm64)."

    #expect(ErrorClassifier.classify(stderr) == .unsupportedHardware("VibeVoice requires Apple Silicon"))
}

@Test
func classifierDetectsAPIError() {
    let stderr = "Error: OpenAI API request failed (HTTP 429)."

    #expect(ErrorClassifier.classify(stderr) == .apiError("OpenAI API request failed (HTTP 429)."))
}

@Test
func classifierIncludesAPIErrorDetailLine() {
    let stderr = """
    Error: OpenAI API request failed (HTTP 429).
    API error (insufficient_quota): You exceeded your current quota.
    """

    let expected = "OpenAI API request failed (HTTP 429). "
        + "API error (insufficient_quota): You exceeded your current quota."
    #expect(ErrorClassifier.classify(stderr) == .apiError(expected))
}

@Test
func classifierPassesUnknownErrorThrough() {
    let stderr = "Traceback (most recent call last):\n  something exploded\n"

    #expect(ErrorClassifier.classify(stderr) == .unknown("Traceback (most recent call last):\n  something exploded"))
}

@Test
func classifierScansMultiLineStderrForMostSpecificMatch() {
    let stderr = """
    INFO: Converting input to mono 16kHz WAV...
    WARNING: something incidental happened
    Error: OPENAI_API_KEY is not set.
    Run: export OPENAI_API_KEY=sk-...
    """

    #expect(ErrorClassifier.classify(stderr) == .missingAPIKey("OPENAI_API_KEY"))
}

@Test
func classifierPrefersMoreSpecificMatchOverLaterGenericOne() {
    let stderr = """
    ERROR: Whisper model not found: ggml-large-v3.bin
    Error: OpenAI API request failed (HTTP 500).
    """

    #expect(ErrorClassifier.classify(stderr) == .missingModel("ggml-large-v3.bin"))
}

@Test
func classifierHandlesWindowsLineEndingsDuringExtraction() {
    let stderr = "INFO: Converting input...\r\nERROR: Whisper model not found: /opt/models/ggml-large-v3.bin\r\n"

    #expect(ErrorClassifier.classify(stderr) == .missingModel("/opt/models/ggml-large-v3.bin"))
}
