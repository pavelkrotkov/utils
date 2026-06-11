# Build & install automation for TranscriptionLauncher.
#
# One-time setup: requires macOS with the Xcode Command Line Tools
# (provides swift, iconutil, and codesign):
#
#     xcode-select --install
#
# Targets:
#   make app      Build the release .app bundle into dist/
#   make install  Copy the .app to /Applications
#   make clean    Remove build artifacts (dist/ and .build/)
#
# `make app` delegates to Scripts/make-app.sh, which builds with SwiftPM,
# assembles the bundle (Info.plist, AppIcon.icns, executable), and applies
# an ad-hoc code signature. Pass CONFIGURATION=debug for a debug build.

CONFIGURATION ?= release
APP_NAME := TranscriptionLauncher
APP := dist/$(APP_NAME).app
INSTALL_DIR ?= /Applications

.PHONY: app install clean

app:
	./Scripts/make-app.sh $(CONFIGURATION)

install: app
	@test -n "$(INSTALL_DIR)" || { echo "Error: INSTALL_DIR must not be empty" >&2; exit 1; }
	rm -rf "$(INSTALL_DIR)/$(APP_NAME).app"
	ditto "$(APP)" "$(INSTALL_DIR)/$(APP_NAME).app"
	@echo "Installed $(INSTALL_DIR)/$(APP_NAME).app"

clean:
	rm -rf dist .build
