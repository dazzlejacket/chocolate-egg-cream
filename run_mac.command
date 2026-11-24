#!/usr/bin/env bash
# Double-click this file on macOS (or run ./run_mac.command in Terminal)
# to set up and launch Chocolate Egg Cream.

set -euo pipefail

cd "$(dirname "$0")"

echo "============================================"
echo "  Chocolate Egg Cream — Mac Launcher"
echo "============================================"
echo

# Find Python 3.10+
PYTHON=""
for candidate in \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    if "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Python 3.10 or newer is required."
  echo "Install it with Homebrew:  brew install python python-tk"
  echo "Or download from:          https://www.python.org/downloads/macos/"
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Using Python: $($PYTHON --version) ($PYTHON)"

# Create virtual environment and install dependencies if needed
if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment (.venv)..."
  "$PYTHON" -m venv .venv
fi

if ! .venv/bin/python -c "import numpy, requests, shazamio" 2>/dev/null; then
  echo "Installing Python dependencies..."
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
fi

# Build the macOS ScreenCaptureKit audio helper if missing or outdated
HELPER="tools/SystemAudioCapture"
SOURCE="tools/SystemAudioCapture.swift"
if [ ! -x "$HELPER" ] || [ "$SOURCE" -nt "$HELPER" ]; then
  if ! command -v swiftc >/dev/null 2>&1; then
    echo
    echo "Xcode Command Line Tools are needed once to compile the Mac audio helper."
    echo "Run this in Terminal, finish the install prompt, then run this script again:"
    echo "  xcode-select --install"
    echo
    read -r -p "Press Enter to close..."
    exit 1
  fi
  echo "Building macOS system audio helper..."
  swiftc -O -o "$HELPER" "$SOURCE"
fi

echo
echo "Starting Chocolate Egg Cream from source..."
echo "(For standalone macOS use without Terminal permissions, install the .dmg release.)"
echo
exec .venv/bin/python -m chocolate_egg_cream
