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
| All four presets produce the right transcript next to the input | `endToEndEveryPresetWritesItsTranscriptNextToInput` |
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
Paid OpenAI smoke tests require explicit `RUN_OPENAI_SMOKE=1` opt-in.

### Real-backend runs

- [ ] Each of the four presets transcribes a short real recording and the
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
- [ ] A clean profile starts with **Private local**; switching to a cloud
      preset shows the upload/API-cost alert, and Cancel retains the local
      preset without saving a cloud choice.
- [ ] After cloud approval, the chosen preset survives restart without
      repeated alerts. An older saved cloud choice with no approval requires
      consent when Run is requested; cancel must not start a cloud process.
- [ ] Failed cloud API responses never dump raw response bodies or transcripts
      into the log.
- [ ] Refresh Environment picks up shell variables changed since launch.

### First-run

- [ ] A packaged app uses `Contents/Resources/Scripts` despite a saved,
      stale source-checkout preference; Settings does not ask for a repo path.
- [ ] A development `swift run` build still detects or prompts for the
      utils source checkout.
- [ ] App works with no settings changes when the shell environment and
      models are fully configured.

## Preset readiness and first-run verification (#193)

`DependencyCheckerTests` uses fake executables, missing/present scripts,
nonempty/empty model files, Hugging Face cache fixtures, hardware overrides,
credential selection, and the >25 MB cloud conversion boundary. No real
model downloads or paid APIs are used by these checks.

On an actual M1/macOS 14+ machine, **from Finder** (not `swift run`):

- [ ] Start with no `~/models/ggml-large-v3-turbo-q8_0.bin` and no stored
      onboarding choice. Open the `.app` from `/Applications` or `dist`.
      Confirm **Private local** is selected, no OpenAI/HF token is requested,
      and missing tools or model each display a specific fix; scripts are bundled.
- [ ] With Homebrew installed in `/opt/homebrew`, put its `brew shellenv`
      invocation in `~/.zprofile`. Reopen from Finder, run **Recheck**, and
      verify `uv`, `ffmpeg`, and Homebrew's `whisper-cli` are resolved.
- [ ] Follow the model's Terminal command, observe curl's progress, then
      **Recheck**: the model changes from missing to ready. A `.part` file
      alone must not make the check pass.
- [ ] Drop a short recording; approve the `uv` dependency-download prompt.
      On the **cold** run, observe package preparation and actual transcript
      creation; on a **warm** second run, verify the cached packages/model
      are reused (after explicitly confirming overwrite if necessary).
- [ ] Select a nonexistent custom model path, remove/rename `whisper-cli`,
      and test an intentionally incomplete development checkout separately;
      missing required scripts must block **Run** with an appropriate checklist item.
- [ ] Select speaker diarization and cloud presets separately; verify
      `HF_TOKEN` is requested only for speakers and `OPENAI_API_KEY` only
      for cloud. Intel or Rosetta should block the VibeVoice preset.
- [ ] For VibeVoice, download its model using the checklist's explicit
      `hf download` command; verify missing and complete cached snapshots
      are distinguished, and the warm run does not fetch weights again.

### Release / Gatekeeper (issue #202)

- [ ] Install the **published notarized ZIP** from Finder on a clean, nondeveloper
      Apple Silicon Mac running macOS 14 or newer, without Swift or git installed.
- [ ] Gatekeeper opens the app normally from Applications (no quarantine bypass);
      confirm the app has a stapled notarization ticket and Developer ID signature.
- [ ] First-run checklist finds bundled scripts, identifies missing Homebrew
      tools and model, and runs a real short local transcription after setup.
- [ ] Verify first cold and subsequent warm `uv` runs, local-file permissions,
      model download, output location, cancellation, and cloud consent.
- [ ] Record Mac model, OS version, release tag/SHA, and outcomes.

PR CI parses Swift syntax on Linux; native macOS build/test is triggered through
the Swift workflow. Neither substitutes for clean-profile Finder testing.
