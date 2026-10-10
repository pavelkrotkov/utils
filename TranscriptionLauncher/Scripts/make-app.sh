#!/usr/bin/env bash
# Build a macOS app with version-matched transcription scripts.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: this script requires macOS (iconutil and codesign)" >&2
    exit 1
fi

CONFIGURATION="${1:-release}"
PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_DIR="$(cd "$PACKAGE_DIR/.." && pwd)"
APP_NAME=TranscriptionLauncher
DIST_DIR="$PACKAGE_DIR/dist"
APP_DIR="$DIST_DIR/$APP_NAME.app"
APPICONSET="$PACKAGE_DIR/Packaging/AppIcon.appiconset"
BUILD_ARGS=(-c "$CONFIGURATION")
if [[ -n "${APP_ARCH:-}" ]]; then BUILD_ARGS+=(--arch "$APP_ARCH"); fi

cd "$PACKAGE_DIR"
echo "==> Building ($CONFIGURATION)"
swift build "${BUILD_ARGS[@]}"
BIN_PATH="$(swift build "${BUILD_ARGS[@]}" --show-bin-path)"

echo "==> Assembling $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/Scripts"
cp "$BIN_PATH/$APP_NAME" "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$PACKAGE_DIR/Packaging/Info.plist" "$APP_DIR/Contents/Info.plist"
cp "$REPO_DIR/LICENSE" "$APP_DIR/Contents/Resources/LICENSE"
printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"

for script in audio_transcribe_openai.sh audio_transcribe_whisper.py audio_transcribe_vibevoice.py \
              audio_cloud_chunks.py audio_common.py audio_segments.py audio_transcript.py; do
    cp "$REPO_DIR/$script" "$APP_DIR/Contents/Resources/Scripts/$script"
done

if [[ -n "${APP_VERSION:-}" ]]; then
    plutil -replace CFBundleShortVersionString -string "$APP_VERSION" "$APP_DIR/Contents/Info.plist"
    plutil -replace CFBundleVersion -string "$APP_VERSION" "$APP_DIR/Contents/Info.plist"
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
ICONSET_DIR="$TEMP_DIR/AppIcon.iconset"
mkdir -p "$ICONSET_DIR"
find "$APPICONSET" -name 'icon_*.png' -exec cp {} "$ICONSET_DIR/" \;
iconutil -c icns "$ICONSET_DIR" -o "$APP_DIR/Contents/Resources/AppIcon.icns"

SIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
if [[ "$SIGN_IDENTITY" == "-" ]]; then
    codesign --force --sign - "$APP_DIR"
else
    codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP_DIR"
fi
codesign --verify --strict "$APP_DIR"
echo "==> Done: $APP_DIR"
