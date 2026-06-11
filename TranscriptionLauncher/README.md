# TranscriptionLauncher

A macOS SwiftUI app for launching the audio-transcription scripts in this
repository (drag-and-drop input, preset picker, live progress log).

## Development

```sh
swift build
swift test
```

## Building the app bundle

The package builds a plain command-line executable; `Scripts/make-app.sh`
wraps it into a double-clickable `.app` (no Xcode project needed):

```sh
Scripts/make-app.sh            # release build (default)
Scripts/make-app.sh debug      # debug build
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
