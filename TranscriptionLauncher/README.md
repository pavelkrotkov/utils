# TranscriptionLauncher

A macOS SwiftUI app for launching the audio-transcription scripts in this
repository (drag-and-drop input, preset picker, live progress log).

## First local transcription (macOS 14+, Apple Silicon)

The app runs scripts from a separate `utils` checkout. It does **not** install
Homebrew tools or download models without your action. On first launch, choose
**Private local** (the default), follow the preset-specific checklist, then
click **Recheck**. Run only commands you approve in Terminal; the commands
below display installation or download progress.

1. Install [Homebrew](https://brew.sh/) if needed, then prepare a checkout:

   ```sh
   git clone https://github.com/pavelkrotkov/utils.git
   brew install uv ffmpeg whisper.cpp
   ```

   Homebrew installs `whisper-cli`, which the Python script uses automatically
   when `whisper-cpp` is absent. Use **Choose Folder** to select the cloned
   `utils` directory. `uv` manages Python dependencies from each script's
   PEP 723 metadata; no manual `pip install` is required.

2. Download the default Whisper model (about **874 MB**), preserving an
   incomplete download as `.part` rather than treating it as a ready model:

   ```sh
   mkdir -p "$HOME/models"
   curl -fL --progress-bar "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin" -o "$HOME/models/ggml-large-v3-turbo-q8_0.bin.part" && mv "$HOME/models/ggml-large-v3-turbo-q8_0.bin.part" "$HOME/models/ggml-large-v3-turbo-q8_0.bin"
   ```

   Or use **Settings → Whisper Model** to select an existing readable `.bin`
   file by absolute path. **Private local** requires no API key or HF token.

3. Build and launch the Finder app (see below). Check that the checklist shows
   the script, `uv`, `ffmpeg`, `whisper-cli`/`whisper-cpp`, and model as ready.
   Drop a short audio file and choose **Run**. The app asks for consent before
   `uv` runs; on cold start, it may fetch Python dependencies (including
   PyTorch/pyannote even for plain Whisper, as declared by the existing
   script). The log shows subprocess output. Repeated warm runs reuse `uv`'s
   cache and the model file.

**Finder / Apple Silicon PATH:** If Terminal can find `uv` or `whisper-cli` but
Finder cannot, ensure Homebrew's shell setup is in `~/.zprofile`:

```sh
eval "$(/opt/homebrew/bin/brew shellenv)"
```

The app captures your login-shell environment and has **Recheck** and
**Settings → Refresh Environment** actions. Changing a preset rechecks only
its requirements: cloud presets need `curl`, `jq` and `OPENAI_API_KEY` (plus
`ffmpeg` for inputs over 25 MB); **Private local with speakers** additionally
needs `HF_TOKEN` and acceptance of the pyannote model terms. **Apple Silicon
local** requires native arm64 and the public 5.7 GB VibeVoice model cached in
Hugging Face's model cache; the checklist provides an explicit `uvx ... hf
download` command with progress. The app never starts a transcription with
missing required items.

## Development

```sh
swift build
swift test
```

Pull requests parse Swift sources on Linux; this is **syntax-only**, not a
SwiftUI/AppKit build. Full native Swift tests and `.app` packaging run on the
Apple Silicon (`macos-15`, M1/arm64) GitHub runner **on demand**, rather than
blocking every PR. For launcher changes or before a release, run:

```sh
gh workflow run swift.yml --repo pavelkrotkov/utils --ref main
```

For an unmerged PR use `--ref <branch>`. Alternatively, open
[Actions → Swift](https://github.com/pavelkrotkov/utils/actions/workflows/swift.yml),
select **Run workflow**, choose the branch, and run it. Verify the `macos`
job passes `swift test`, app assembly, `plutil` and `codesign`. Rerunning
an existing job tests its original commit; dispatch a new workflow for the
latest branch head. Locally, use `swift test && make app` from this directory;
[Finder cold/warm validation](TESTING.md) remains a separate manual check.

## Building the app bundle

The package builds a plain command-line executable; the `Makefile` wraps
it into a double-clickable `.app` (no Xcode project needed):

```sh
make app          # Build release dist/TranscriptionLauncher.app
make install      # Copy the built .app to /Applications (run make app first)
make clean        # Remove build artifacts (dist/ and .build/)
```

`make app` delegates to `Scripts/make-app.sh`, which can also be invoked
directly:

```sh
Scripts/make-app.sh            # release build (default)
Scripts/make-app.sh debug      # debug build (or: make app CONFIGURATION=debug)
```

The script builds with SwiftPM, assembles `dist/TranscriptionLauncher.app`
with `Packaging/Info.plist` and an `AppIcon.icns` generated from
`Packaging/AppIcon.appiconset`, and applies an ad-hoc code signature —
sufficient for local use without notarization.

Packaging decisions:

- **Bundle identifier**: `com.pavelkrotkov.TranscriptionLauncher`
- **Regular Dock app** (no `LSUIElement`): the app is a windowed
  drag-and-drop tool and explicitly activates as a regular app at launch.
- **No App Sandbox**: the app runs repo scripts via `uv` and reads/writes
  arbitrary user-chosen paths, so it is intentionally unsandboxed and
  signed without entitlements.

## App icon

The icon PNGs in `Packaging/AppIcon.appiconset` are committed. To
regenerate them after changing the artwork:

```sh
uv run Scripts/generate_icon.py
```
