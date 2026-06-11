#!/usr/bin/env bash
# Assemble TranscriptionLauncher.app from the SwiftPM build output.
#
# Builds the release binary, lays out the .app bundle (Info.plist, icon,
# executable), converts the committed icon PNGs into AppIcon.icns with
# iconutil, and applies an ad-hoc code signature. The result lands in
# TranscriptionLauncher/dist/TranscriptionLauncher.app.
#
# Usage: Scripts/make-app.sh [debug|release]   (default: release)

set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: this script requires macOS (iconutil and codesign)" >&2
    exit 1
fi

CONFIGURATION="${1:-release}"
PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="TranscriptionLauncher"
DIST_DIR="$PACKAGE_DIR/dist"
APP_DIR="$DIST_DIR/$APP_NAME.app"
APPICONSET="$PACKAGE_DIR/Packaging/AppIcon.appiconset"

cd "$PACKAGE_DIR"

echo "==> Building ($CONFIGURATION)"
swift build -c "$CONFIGURATION"
BIN_PATH="$(swift build -c "$CONFIGURATION" --show-bin-path)"

echo "==> Assembling $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cp "$BIN_PATH/$APP_NAME" "$APP_DIR/Contents/MacOS/$APP_NAME"
cp "$PACKAGE_DIR/Packaging/Info.plist" "$APP_DIR/Contents/Info.plist"
printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"

echo "==> Building AppIcon.icns"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
ICONSET_DIR="$TEMP_DIR/AppIcon.iconset"
mkdir -p "$ICONSET_DIR"
# iconutil expects icon_<size>.png / icon_<size>@2x.png names, which is
# exactly how the appiconset PNGs are named — copy everything but the
# asset-catalog manifest.
find "$APPICONSET" -name 'icon_*.png' -exec cp {} "$ICONSET_DIR/" \;
iconutil -c icns "$ICONSET_DIR" -o "$APP_DIR/Contents/Resources/AppIcon.icns"

# Ad-hoc signature: enough for local use; no entitlements on purpose —
# the app is unsandboxed so it can run repo scripts and read/write
# arbitrary paths (see issue #66).
echo "==> Code signing (ad-hoc)"
codesign --force --sign - "$APP_DIR"
codesign --verify --strict "$APP_DIR"

echo "==> Done: $APP_DIR"
