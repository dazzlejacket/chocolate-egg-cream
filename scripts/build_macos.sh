#!/usr/bin/env bash
# Build standalone macOS .app bundle and drag-to-Applications .dmg.
#
# Bundles Python, Tkinter, ShazamIO, all dependencies, and the pre-compiled
# Core Audio Taps / ScreenCaptureKit helper (SystemAudioCapture) inside
# Chocolate Egg Cream.app/Contents/MacOS/.
#
# Optional signing & notarization environment variables:
#   APPLE_SIGNING_IDENTITY  e.g. "Developer ID Application: Name (TEAMID)"
#   APPLE_ID                Apple ID email for notarytool
#   APPLE_TEAM_ID           10-character Apple Developer Team ID
#   APPLE_APP_PASSWORD      App-specific password for notarytool
#
# Without APPLE_SIGNING_IDENTITY, the bundle is ad-hoc signed ("-") and
# Gatekeeper limitations are documented clearly.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
  ARCH_LABEL="arm64"
  SWIFT_TARGET="arm64-apple-macos13.0"
else
  ARCH_LABEL="x86_64"
  SWIFT_TARGET="x86_64-apple-macos13.0"
fi

VERSION="$("$PYTHON" -c "import pathlib, re; t = pathlib.Path('chocolate_egg_cream/__init__.py').read_text(); print(re.search(r'__version__\s*=\s*[\"\x27]([^\"\x27]+)', t).group(1))")"
echo "Building Chocolate Egg Cream $VERSION for macOS ($ARCH_LABEL)..."

# 1. Compile the native Swift system audio helper for the target architecture.
echo "Compiling tools/SystemAudioCapture ($SWIFT_TARGET)..."
swiftc -O -target "$SWIFT_TARGET" -o tools/SystemAudioCapture tools/SystemAudioCapture.swift
chmod +x tools/SystemAudioCapture
file tools/SystemAudioCapture

# 2. Build Chocolate Egg Cream.app with PyInstaller.
rm -rf build dist
"$PYTHON" -m PyInstaller --clean --noconfirm packaging/chocolate_egg_cream.spec

APP_BUNDLE="dist/Chocolate Egg Cream.app"
HELPER_IN_APP="$APP_BUNDLE/Contents/MacOS/SystemAudioCapture"

if [ ! -d "$APP_BUNDLE" ]; then
  echo "ERROR: Expected app bundle not found at $APP_BUNDLE" >&2
  exit 1
fi

# Ensure SystemAudioCapture sits directly in Contents/MacOS/ and is executable.
if [ ! -x "$HELPER_IN_APP" ]; then
  cp tools/SystemAudioCapture "$HELPER_IN_APP"
  chmod +x "$HELPER_IN_APP"
fi

# Verify Info.plist has our bundle identity and audio-only permission key.
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP_BUNDLE/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Print :NSAudioCaptureUsageDescription" "$APP_BUNDLE/Contents/Info.plist"

# 3. Code signing (Developer ID if configured, otherwise ad-hoc).
SIGN_ID="${APPLE_SIGNING_IDENTITY:-}"
if [ -n "$SIGN_ID" ]; then
  echo "Signing with Developer ID identity: $SIGN_ID"
  codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$HELPER_IN_APP"
  codesign --force --deep --options runtime --timestamp --sign "$SIGN_ID" "$APP_BUNDLE"
else
  echo "No APPLE_SIGNING_IDENTITY set; performing ad-hoc signing."
  echo "Note: Ad-hoc signed apps require right-click -> Open (or Privacy & Security approval) on first launch."
  codesign --force --sign - "$HELPER_IN_APP"
  codesign --force --deep --sign - "$APP_BUNDLE"
fi

codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"

# 4. Create drag-to-Applications .dmg.
RELEASE_DIR="dist/release"
mkdir -p "$RELEASE_DIR"
DMG_STAGING="dist/dmg-staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "$APP_BUNDLE" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

ARTIFACT_NAME="ChocolateEggCream-${VERSION}-macos-${ARCH_LABEL}.dmg"
ARTIFACT_PATH="${RELEASE_DIR}/${ARTIFACT_NAME}"

hdiutil create \
  -volname "Chocolate Egg Cream" \
  -srcfolder "$DMG_STAGING" \
  -ov \
  -format UDZO \
  "$ARTIFACT_PATH"

rm -rf "$DMG_STAGING"

# 5. Optional notarization & stapling when Developer ID + Apple credentials are provided.
if [ -n "$SIGN_ID" ] && [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]; then
  echo "Signing DMG and submitting for Apple notarization..."
  codesign --force --timestamp --sign "$SIGN_ID" "$ARTIFACT_PATH"
  xcrun notarytool submit "$ARTIFACT_PATH" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --wait
  xcrun stapler staple "$ARTIFACT_PATH"
  xcrun stapler validate "$ARTIFACT_PATH"
fi

# 6. Checksum.
(
  cd "$RELEASE_DIR"
  shasum -a 256 "$ARTIFACT_NAME" > "${ARTIFACT_NAME}.sha256"
)

echo "Created: $ARTIFACT_PATH ($(du -h "$ARTIFACT_PATH" | cut -f1))"
cat "${ARTIFACT_PATH}.sha256"
