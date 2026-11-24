"""Linux system audio: PipeWire / PulseAudio monitor source.

Both PipeWire (through its PulseAudio compatibility layer) and plain
PulseAudio expose a `<sink>.monitor` source that carries exactly what the
sink is playing. soundcard talks to PulseAudio, so the monitor source is
what we open -- the microphone is never used.
"""

from __future__ import annotations

import threading

import numpy as np

from .base import AudioCapture


class LinuxCapture(AudioCapture):
    """Captures the monitor source of the default output sink."""

    def __init__(self, sample_rate: int = 48000, device_id: str | None = None) -> None:
        self.sample_rate = sample_rate
        self._device_id = device_id
        self._recorder = None
        self._channels = 2
        self._stop_event = threading.Event()

    def open(self) -> None:
        import soundcard as sc

        self._stop_event.clear()
        sources = sc.all_microphones(include_loopback=True)
        if self._device_id:
            matches = [s for s in sources if self._device_id in s.name]
            if not matches:
                raise RuntimeError(f"No PulseAudio monitor source matching {self._device_id!r}")
            monitor = matches[0]
        else:
            monitor = self._pick_monitor(sc, sources)

        self._channels = min(2, max(1, monitor.channels))
        self._recorder = monitor.recorder(
            samplerate=self.sample_rate, channels=self._channels
        )
        self._recorder.__enter__()

    def request_stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _pick_monitor(sc, sources: list) -> object:
        """Prefer the monitor of the default sink; fall back to any monitor."""
        default_sink = sc.default_speaker().name
        monitors = [s for s in sources if getattr(s, "isloopback", False)]
        if not monitors:
            raise RuntimeError(
                "No PulseAudio monitor source found. On PipeWire/PulseAudio a "
                "monitor source (e.g. 'alsa_output.pci-0000_00_1f.3.analog-stereo.monitor') "
                "must be available for system-audio capture."
            )
        for monitor in monitors:
            if default_sink and default_sink in monitor.name:
                return monitor
        return monitors[0]

    def read(self, seconds: float) -> np.ndarray:
        if self._recorder is None:
            raise RuntimeError("Linux capture is not open")
        total_frames = int(self.sample_rate * seconds)
        chunk_frames = max(1, int(self.sample_rate * 0.5))
        chunks: list[np.ndarray] = []
        remaining = total_frames
        while remaining > 0 and not self._stop_event.is_set():
            take = min(chunk_frames, remaining)
            data = np.asarray(self._recorder.record(numframes=take), dtype=np.float32)
            if data.ndim > 1:
                data = data.mean(axis=1)
            chunks.append(data)
            remaining -= take
        if not chunks or self._stop_event.is_set():
            return np.zeros(0, dtype=np.float32)
        return chunks[0] if len(chunks) == 1 else np.concatenate(chunks)

    def close(self) -> None:
        self._stop_event.set()
        if self._recorder is not None:
            self._recorder.__exit__(None, None, None)
            self._recorder = None
