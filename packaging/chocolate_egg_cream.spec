# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform PyInstaller spec for Chocolate Egg Cream.

Builds:
  - Windows: standalone single-file windowed EXE (no console window)
  - macOS:   Chocolate Egg Cream.app bundle with embedded SystemAudioCapture helper
             and NSAudioCaptureUsageDescription (audio-only Core Audio Taps)
  - Linux:   self-contained onedir bundle (packaged into AppImage / tar.gz)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
APP_NAME = "Chocolate Egg Cream"
BUNDLE_ID = "com.chocolateeggcream.app"

# Read version without importing heavy runtime deps.
VERSION = "1.0.0"
init_file = ROOT / "chocolate_egg_cream" / "__init__.py"
for line in init_file.read_text(encoding="utf-8").splitlines():
    if line.startswith("__version__"):
        VERSION = line.split("=", 1)[1].strip().strip('"').strip("'")
        break

datas = []
binaries = []
hiddenimports = [
    "chocolate_egg_cream",
    "chocolate_egg_cream.capture",
    "chocolate_egg_cream.scrobble",
    "chocolate_egg_cream.scrobble.lastfm",
    "chocolate_egg_cream.scrobble.listenbrainz",
    "tkinter",
    "tkinter.ttk",
]

# Bundle shazamio + shazamio_core (native Rust extension) + certifi CA certs.
datas += collect_data_files("certifi")
datas += collect_data_files("shazamio")
binaries += collect_dynamic_libs("shazamio_core")
hiddenimports += collect_submodules("shazamio")
hiddenimports += collect_submodules("shazamio_core")

if sys.platform != "darwin":
    # soundcard loads C headers (.py.h) at runtime via cffi.
    datas += collect_data_files("soundcard")
    hiddenimports += ["soundcard", "_cffi_backend"]
    if sys.platform == "win32":
        hiddenimports.append("chocolate_egg_cream.capture.windows")
    elif sys.platform.startswith("linux"):
        hiddenimports.append("chocolate_egg_cream.capture.linux")
else:
    hiddenimports.append("chocolate_egg_cream.capture.macos")
    # Bundle pre-compiled macOS SystemAudioCapture Swift helper into Contents/MacOS/.
    helper_bin = ROOT / "tools" / "SystemAudioCapture"
    if not helper_bin.is_file():
        raise FileNotFoundError(
            f"Compiled macOS audio helper not found at {helper_bin}. "
            "Run scripts/build_macos.sh or compile tools/SystemAudioCapture.swift first."
        )
    binaries.append((str(helper_bin), "."))

excludes = [
    "matplotlib",
    "PIL",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "tkinter.test",
    "test",
]

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# On Linux, never bundle host audio libraries so the AppImage/bundle uses the
# user's native PipeWire or PulseAudio client library.
if sys.platform.startswith("linux"):
    skip_prefixes = ("libpulse", "libasound", "libpipewire", "libjack", "libsndfile")
    a.binaries = [
        b for b in a.binaries
        if not os.path.basename(b[0]).startswith(skip_prefixes)
    ]

pyz = PYZ(a.pure)

if sys.platform == "win32":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
    )
elif sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.music",
            # Core Audio Taps (macOS 14.2+) audio-only system capture permission.
            # Explicitly no NSMicrophoneUsageDescription or NSCameraUsageDescription.
            "NSAudioCaptureUsageDescription": (
                "Chocolate Egg Cream captures system audio to identify playing "
                "songs and scrobble them. It never captures your screen or microphone."
            ),
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="chocolate-egg-cream",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="chocolate-egg-cream",
    )
