"""macOS system audio capture via Core Audio Taps (macOS 14.2+) or ScreenCaptureKit fallback.

System audio is captured by a small native Swift helper (`SystemAudioCapture`)
that writes mono 16-bit little-endian PCM to stdout. It never captures screen
video and never touches the microphone.

On macOS 14.2+, Core Audio Taps (`AudioHardwareCreateProcessTap`) are used by
default, requiring only the audio-only `NSAudioCaptureUsageDescription`
("System Audio Recording Only") permission for the Chocolate Egg Cream app
bundle itself — never Terminal.

On macOS 13.0–14.1 (or when explicitly selected with `backend="screencapturekit"`),
ScreenCaptureKit is used as a compatibility fallback.

In packaged `.app` builds, the compiled `SystemAudioCapture` binary is bundled
inside `Contents/MacOS/` and is never compiled at runtime. When running from
source, it can be built with:

    swiftc -O -o tools/SystemAudioCapture tools/SystemAudioCapture.swift
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .base import AudioCapture

log = logging.getLogger(__name__)

_HELPER_NAME = "SystemAudioCapture"
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
VALID_BACKENDS = ("auto", "coreaudio", "screencapturekit")


def _bundled_binary_candidates() -> list[Path]:
    """Locations where the pre-compiled helper lives inside a packaged .app."""
    candidates: list[Path] = []
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / _HELPER_NAME)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.append(mp / _HELPER_NAME)
        candidates.append(mp / "tools" / _HELPER_NAME)
    # Standard macOS .app layout: Contents/MacOS/SystemAudioCapture
    candidates.append(exe_dir.parent / "MacOS" / _HELPER_NAME)
    return candidates


def _source_candidates() -> list[Path]:
    """Places the Swift source may live in when running from source."""
    return [
        _PACKAGE_ROOT.parent / "tools" / "SystemAudioCapture.swift",
        _PACKAGE_ROOT / "tools" / "SystemAudioCapture.swift",
        Path.cwd() / "tools" / "SystemAudioCapture.swift",
    ]


def _binary_path(source: Path | None) -> Path:
    """Preferred location for the compiled helper binary when running from source."""
    if source is not None and os.access(source.parent, os.W_OK):
        return source.parent / _HELPER_NAME
    fallback_dir = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "chocolate-egg-cream"
    )
    fallback_dir.mkdir(parents=True, exist_ok=True)
    return fallback_dir / _HELPER_NAME


def helper_path() -> Path:
    """Locate the native macOS audio helper binary.

    In packaged builds (`sys.frozen`), returns the bundled binary directly and
    never attempts runtime compilation. When running from source, builds or
    rebuilds the binary if `swiftc` is available.
    """
    if getattr(sys, "frozen", False):
        for candidate in _bundled_binary_candidates():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        raise RuntimeError(
            "Bundled macOS audio helper (SystemAudioCapture) is missing from the "
            "application bundle. Please re-install Chocolate Egg Cream."
        )

    source = next((path for path in _source_candidates() if path.exists()), None)
    binary = _binary_path(source)

    if binary.exists():
        if source is None or binary.stat().st_mtime >= source.stat().st_mtime:
            return binary

    if source is None:
        raise RuntimeError(
            "The macOS audio helper source (tools/SystemAudioCapture.swift) was "
            "not found. Keep it next to the chocolate_egg_cream package and run:\n"
            f"  swiftc -O -o {binary} tools/SystemAudioCapture.swift"
        )

    compiler = shutil.which("swiftc")
    if compiler is None:
        raise RuntimeError(
            "Running from source on macOS requires Xcode Command Line Tools to "
            "build the audio helper (`xcode-select --install`), or download the "
            "standalone Chocolate Egg Cream.dmg release."
        )

    log.info("Building the macOS audio helper with %s ...", compiler)
    result = subprocess.run(
        [compiler, "-O", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not build the macOS audio helper:\n{result.stderr.strip()}")
    return binary


class MacCapture(AudioCapture):
    """Captures macOS system audio via Core Audio Taps (default) or ScreenCaptureKit."""

    def __init__(self, sample_rate: int = 48000, backend: str = "auto") -> None:
        self.sample_rate = sample_rate
        self.backend = backend if backend in VALID_BACKENDS else "auto"
        self._process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._recording = False
        self._chunks: deque[bytes] = deque()
        self._buffered_bytes = 0
        self._stderr_lines: list[str] = []
        self._info_lines: list[str] = []
        self._eof = False

    def open(self) -> None:
        binary = helper_path()
        self._chunks.clear()
        self._buffered_bytes = 0
        self._stderr_lines.clear()
        self._info_lines.clear()
        self._eof = False
        self._recording = False

        cmd = [
            str(binary),
            "--rate",
            str(self.sample_rate),
            "--backend",
            self.backend,
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="mac-audio-stderr", daemon=True
        )
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, name="mac-audio-stdout", daemon=True
        )
        self._stderr_thread.start()
        self._stdout_thread.start()

        # Give the helper a brief moment to fail fast on permission errors.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                err = self._error_text()
                self.close()
                raise RuntimeError(f"Could not start macOS audio capture: {err}")
            time.sleep(0.05)

    def _drain_stderr(self) -> None:
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("info:"):
                    detail = line[5:].strip()
                    log.info("macOS audio helper: %s", detail)
                    with self._lock:
                        self._info_lines.append(detail)
                else:
                    with self._lock:
                        self._stderr_lines.append(line)
        except Exception:  # noqa: BLE001
            pass

    def _drain_stdout(self) -> None:
        """Continuously read stdout so the pipe never fills with stale audio."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                block = proc.stdout.read(8192)
                if not block:
                    break
                with self._condition:
                    if self._recording:
                        self._chunks.append(block)
                        self._buffered_bytes += len(block)
                        self._condition.notify_all()
        except Exception:  # noqa: BLE001
            pass
        finally:
            with self._condition:
                self._eof = True
                self._condition.notify_all()

    def read(self, seconds: float) -> np.ndarray:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("The macOS capture is not running")

        wanted = int(self.sample_rate * seconds) * 2  # mono int16
        timeout = max(10.0, seconds + 10.0)
        deadline = time.monotonic() + timeout

        eof_error = False
        timed_out = False
        collected = bytearray()

        with self._condition:
            # Start fresh so we only capture live audio from right now.
            self._chunks.clear()
            self._buffered_bytes = 0
            self._recording = True
            try:
                while self._buffered_bytes < wanted and not self._eof:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)

                if self._buffered_bytes < wanted:
                    eof_error = self._eof
                    timed_out = not self._eof
                else:
                    while len(collected) < wanted and self._chunks:
                        chunk = self._chunks.popleft()
                        need = wanted - len(collected)
                        if len(chunk) <= need:
                            collected.extend(chunk)
                            self._buffered_bytes -= len(chunk)
                        else:
                            collected.extend(chunk[:need])
                            self._chunks.appendleft(chunk[need:])
                            self._buffered_bytes -= need
            finally:
                self._recording = False
                self._chunks.clear()
                self._buffered_bytes = 0

        if eof_error:
            raise RuntimeError("The macOS audio helper stopped: " + self._error_text())
        if timed_out:
            raise RuntimeError(
                "Timed out waiting for macOS system audio. Make sure Chocolate Egg Cream "
                "is allowed under System Settings -> Privacy & Security -> Screen & System "
                "Audio Recording (System Audio Recording Only), and that audio is playing."
            )

        samples = np.frombuffer(bytes(collected), dtype="<i2").astype(np.float32) / 32768.0
        return samples

    def _error_text(self) -> str:
        # Give stderr thread a moment to flush the final error line.
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.5)
        with self._lock:
            if self._stderr_lines:
                return " ".join(self._stderr_lines)
        return "the helper exited without an error message"

    def request_stop(self) -> None:
        with self._condition:
            self._recording = False
            self._eof = True
            self._condition.notify_all()

    def close(self) -> None:
        proc = self._process
        self._process = None
        with self._condition:
            self._recording = False
            self._eof = True
            self._condition.notify_all()
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
