"""System audio capture, one implementation per platform."""

from __future__ import annotations

import sys

from .base import AudioCapture

__all__ = ["AudioCapture", "get_capture"]


def get_capture(sample_rate: int = 48000) -> AudioCapture:
    """Return the capture implementation for the current platform."""
    if sys.platform == "win32":
        from .windows import WindowsCapture

        return WindowsCapture(sample_rate=sample_rate)
    if sys.platform == "darwin":
        from .macos import MacCapture

        return MacCapture(sample_rate=sample_rate)
    if sys.platform.startswith("linux"):
        from .linux import LinuxCapture

        return LinuxCapture(sample_rate=sample_rate)
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
