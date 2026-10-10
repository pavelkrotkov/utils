# TranscriptionLauncher

A macOS SwiftUI app for launching the audio-transcription scripts in this
repository (drag-and-drop input, preset picker, live progress log).

## Download and install (macOS 14+, Apple Silicon)

Download `TranscriptionLauncher-<version>-macos-arm64.zip` from
[GitHub Releases](https://github.com/pavelkrotkov/utils/releases),
open it in Finder, move `TranscriptionLauncher.app` to Applications, and
double-click to launch. Published releases are Developer ID signed and
notarized; no Swift build or utils checkout is required. If Gatekeeper
rejects the app, verify the file came from the official release rather than
bypassing the warning. You still install local tools/models separately.

## Outputs

Choose **Format** alongside the preset. Cloud supports plain text and Markdown;
Whisper adds SRT/VTT subtitles; Whisper with speakers and VibeVoice also offer
speaker-labeled text. Markdown records the original words, available timestamps
and speakers, and a relative link to the source audio (or source JSON). It
does **not** invent summaries or study notes. Copy the audio alongside the
Markdown file if moving both into an Obsidian vault.

For VibeVoice, enable **Keep structured JSON** to save
`<recording>.vibevoice.json` alongside the selected reading format. A
single-pass run preserves the native model JSON; a chunked run saves normalized
merged segments, with speaker identities resetting at chunk boundaries.
To export again without transcription, drop the saved `.vibevoice.json` in
the launcher and select another format. Re-export uses Python's standard
library, without VibeVoice model weights or Apple Silicon.

Output names distinguish formats (`.srt`, `.vtt`, `.md`,
`.spk.txt`, `.vibevoice.spk.txt`). The launcher confirms before replacing
any existing output, including optional JSON. Without **Keep structured JSON**,
a later run does not touch an existing saved JSON.

## First local transcription (macOS 14+, Apple Silicon)

The released app contains the transcription scripts from its own versioned build.
It does **not** install Homebrew tools or download models without your action.
On first launch, choose **Private local** (the default), follow the preset-specific
checklist, then click **Recheck**. Run only commands you approve in Terminal;
the commands below display installation or download progress.

The **OpenAI cloud** preset uses `gpt-transcribe` for plain-text transcripts;
it uploads audio, requires your own `OPENAI_API_KEY`, and is billed by OpenAI
(about **$0.0045/minute** as of October 2026). Files are limited to 25 MB per
request; the script first compresses oversized files with ffmpeg. The launcher
requests consent before the first cloud upload and remembers approval.
Previously saved fast/best/compatible cloud choices migrate to this single cloud
preset, preserving the consent requirement. Speaker labels and timestamps are
not provided by this preset.

1. Install [Homebrew](https://brew.sh/) if needed, then the local tools:

   ```sh
   brew install uv ffmpeg whisper.cpp
   ```

   Homebrew installs `whisper-cli`, which the Python script uses automatically
   when `whisper-cpp` is absent. `uv` manages script dependencies; no manual
   `pip install` or repository checkout is required.

2. Download the default Whisper model (about **874 MB**), preserving an
   incomplete download as `.part` rather than treating it as a ready model:

   ```sh
   mkdir -p "$HOME/models"
   curl -fL --progress-bar "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin" -o "$HOME/models/ggml-large-v3-turbo-q8_0.bin.part" && mv "$HOME/models/ggml-large-v3-turbo-q8_0.bin.part" "$HOME/models/ggml-large-v3-turbo-q8_0.bin"
   ```

   Or use **Settings → Whisper Model** to select an existing readable `.bin`
   file by absolute path. **Private local** requires no API key or HF token.

3. Open the installed app from Finder. Check that the checklist shows
   the bundled script, `uv`, `ffmpeg`, `whisper-cli`/`whisper-cpp`, and model as ready.
   Drop a short audio file and choose **Run**. The app asks for consent before
   `uv` runs. Plain Whisper needs no additional Python packages; the speakers
   preset installs pyannote and PyTorch on demand through `uv`. The log shows
   subprocess output. Warm runs reuse `uv`'s cache and the model file.

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

Under **Settings → VibeVoice Processing**, single pass (default) lets VibeVoice
track speakers across the recording. For low-memory machines, choose 1-, 2-,
or 5-minute chunks; speaker labels reset at every boundary and cannot identify
people consistently across the full recording. Shorter chunks reduce the
per-pass memory demand, but even a 5-minute chunk may OOM on 16 GB. A full
37-minute transcription on an M1/16 GB Mac has not yet been verified; that
hardware check remains separate from unit tests.

## Development

```sh
swift build
swift test
```

PR CI parses Swift on Linux; this is **not** a native build. For every app
source, test, build, or packaging change, agents must dispatch the `Swift`
workflow on the current PR branch before merging, using
`gh workflow run swift.yml --repo pavelkrotkov/utils --ref YOUR_PR_BRANCH`
or **Actions → Swift → Run workflow**. Alternatively, mark a same-repo draft
PR **Ready for review** to trigger native validation of its current head SHA
(no CLI needed); re-draft and mark ready again after further changes.
Verify its M1 `macos-15` job passes native tests, packaging, and signature checks.
Re-dispatch after changes:
rerunning an old job tests its original commit. Documentation-only and
unrelated Python changes do not require native Swift validation. See
[AGENTS.md](../AGENTS.md) for the merge rule.

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
sufficient for local development without notarization. The app includes
the seven transcription scripts and the root MIT license in
`Contents/Resources`; it does not bundle CLI tools, Python packages, or
model weights. Development builds can still select a source checkout.

## Publishing a notarized release

Configure the repository Actions secrets `APPLE_CERTIFICATE_P12` (base64
Developer ID Application .p12), `APPLE_CERTIFICATE_PASSWORD`,
`APPLE_CODESIGN_IDENTITY` (full Developer ID Application identity),
`APPLE_NOTARY_APPLE_ID`, `APPLE_NOTARY_PASSWORD` (app-specific password),
and `APPLE_TEAM_ID`. Protect release tags and the signing credentials.
After merging and verifying native Swift CI, tag the intended main commit
`transcription-vX.Y.Z` and push that tag. The release workflow cross-builds
arm64, signs with hardened runtime, notarizes and staples the app, verifies
Gatekeeper, and only then publishes the versioned ZIP and SHA-256 checksum.
A failing check publishes nothing. The default `make app` remains ad-hoc
signed and is **not** a public distribution artifact.

Before announcing a release, run the clean-Mac Finder checks in
[TESTING.md](TESTING.md); CI does not emulate a nondeveloper macOS profile.

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
