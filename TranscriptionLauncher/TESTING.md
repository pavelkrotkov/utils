# End-to-end testing (#64)

The integration matrix from issue #64 is verified in two layers:

1. **Automated integration tests** (`swift test`, run in CI on every PR) —
   `Tests/TranscriptionLauncherTests/EndToEndPresetTests.swift` drives the
   real pipeline (`CommandBuilder` → `ProcessRunner`) against a fixture
   repo whose scripts are stubs reproducing the real scripts' argument
   interfaces and error messages, with a fake `uv` shim preserving the
   wrapper-process shape.
2. **Manual verification** on a real Mac with the real scripts, API keys,
   and audio files — everything below that cannot run headless in CI.

## Automated coverage

| Matrix item | Test |
| --- | --- |
| All six presets produce the right transcript next to the input | `endToEndEveryPresetWritesItsTranscriptNextToInput` |
| Filenames with spaces, unicode, no extension, multiple dots | `endToEndEdgeCaseFilenames` (plus `OutputPathResolverTests`) |
| Existing output is replaced after confirmation | `endToEndExistingOutputIsOverwritten` |
| Cancel during cloud run → process stops, no partial output | `endToEndCancelledCloudRunLeavesNoOutput` |
| Cancel during local whisper → temp files cleaned up | `endToEndCancelledUVRunCleansUpTempFiles` |
| Cancel kills `uv` and its Python child | `ProcessRunnerTests` (incl. `testCancellationTerminatesPythonChildUnderUV`, auto-enabled where `uv` is installed) |
| Missing `OPENAI_API_KEY` / `HF_TOKEN` / whisper model / `ffmpeg` → friendly error | `endToEndScriptFailuresSurfaceFriendlyErrors` (plus `ErrorClassifierTests`) |
| Missing `uv` → friendly error | `endToEndMissingUVReportsMissingDependency` |
| VibeVoice on Intel Mac → "requires Apple Silicon" | `endToEndScriptFailuresSurfaceFriendlyErrors` |
| OpenAI API failure surfaces actionable detail | `endToEndAPIErrorSurfacedWithDetail` |
| Progress events parsed from live stderr through `uv` | `endToEndProgressEventsFlowThroughUVRun` |
| Repo auto-detection (inside repo, outside repo, symlinks) | `TranscriptionLauncherTests` (RepoDetector) |

## Manual checklist

Run on a real Mac (`make app`, launch `dist/TranscriptionLauncher.app`)
with `OPENAI_API_KEY`/`HF_TOKEN` configured and a few real recordings.

### Real-backend runs

- [ ] Each of the six presets transcribes a short real recording and the
      transcript appears next to the input with the expected suffix
      (`.txt`, `.spk.txt`, `.vibevoice.txt`).
- [ ] Large file (>25MB) triggers the OpenAI script's ffmpeg downsampling
      (watch for the downsampling lines in the log view).

### UX

- [ ] Output file already exists → overwrite confirmation dialog appears
      before the run starts.
- [ ] Finder reveal works after a successful transcription.
- [ ] Progress bar updates during long local whisper runs.
- [ ] Log view scrolls and shows live output.
- [ ] App remains responsive during transcription (no UI freezes).
- [ ] Settings changes persist across app restarts.
- [ ] Refresh Environment picks up shell variables changed since launch.

### First-run

- [ ] App auto-detects repo root when placed inside the repo directory.
- [ ] App prompts for the repo path when launched from outside the repo.
- [ ] App works immediately with no settings changes when the shell
      environment is fully configured.
