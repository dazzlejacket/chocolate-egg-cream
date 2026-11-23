"""macOS system audio: ScreenCaptureKit.

ScreenCaptureKit hands out compressed sample buffers that only native code
can decode, so the capture lives in a small Swift helper
(`tools/SystemAudioCapture.swift`) that writes mono 16-bit PCM to stdout.
This module just pumps that stream.

Build the helper once with:

    swiftc -O -o tools/SystemAudioCapture tools/SystemAudioCapture.swift

If the binary is missing but a Swift compiler is present, it is built
automatically on first use.

Note: macOS asks for Screen Recording permission the first time the
capture starts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

from .base import AudioCapture

_HELPER_NAME = "SystemAudioCapture"
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _source_candidates() -> list[Path]:
    """Places the Swift source may live in."""
    return [
        _PACKAGE_ROOT.parent / "tools" / "SystemAudioCapture.swift",
        _PACKAGE_ROOT / "tools" / "SystemAudioCapture.swift",
        Path.cwd() / "tools" / "SystemAudioCapture.swift",
    ]


def helper_path() -> Path:
    """Path of the Swift helper, building it if needed."""
    binary = _PACKAGE_ROOT.parent / "tools" / _HELPER_NAME
    if binary.exists():
        return binary

    source = next((path for path in _source_candidates() if path.exists()), None)
    if source is None:
        raise RuntimeError(
            "The macOS audio helper source (tools/SystemAudioCapture.swift) was "
            "not found. Keep it next to the chocolate_egg_cream package and run:\n"
            f"  swiftc -O -o {binary} tools/SystemAudioCapture.swift"
        )

    compiler = shutil.which("swiftc")
    if compiler is None:
        raise RuntimeError(
            "The macOS audio helper is not built. Install the Xcode command line "
            "tools and run:\n"
            f"  swiftc -O -o {binary} {source}"
        )

    print(f"Building the macOS audio helper with {compiler} ...")
    result = subprocess.run(
        [compiler, "-O", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not build the macOS audio helper:\n{result.stderr}")
    return binary


class MacCapture(AudioCapture):
    """Captures system audio with ScreenCaptureKit."""

    def __init__(self, sample_rate: int = 48000) -> None:
        self.sample_rate = sample_rate
        self._process: subprocess.Popen | None = None

    def open(self) -> None:
        binary = helper_path()
        self._process = subprocess.Popen(
            [str(binary), "--rate", str(self.sample_rate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def read(self, seconds: float) -> np.ndarray:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("The macOS capture is not running")

        wanted = int(self.sample_rate * seconds) * 2  # mono int16
        chunks = bytearray()
        while len(chunks) < wanted:
            block = self._process.stdout.read(wanted - len(chunks))
            if not block:
                raise RuntimeError(
                    "The macOS audio helper stopped: "
                    + self._error_text()
                )
            chunks.extend(block)

        samples = np.frombuffer(bytes(chunks), dtype="<i2").astype(np.float32) / 32768.0
        return samples

    def _error_text(self) -> str:
        if self._process is None or self._process.stderr is None:
            return "no output"
        try:
            self._process.stderr.flush()
        except Exception:
            pass
        return "the helper did not say why"

    def close(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
