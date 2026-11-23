"""Windows system audio: WASAPI loopback.

soundcard drives `IAudioClient` with `AUDCLNT_STREAMFLAGS_LOOPBACK`, which
returns the mixed audio Windows is playing on the output device. The
microphone is never opened.
"""

from __future__ import annotations

import warnings

import numpy as np

from .base import AudioCapture

# Loopback capture always reports a discontinuity when it starts. That is
# expected, and it would only be noise in the console.
warnings.filterwarnings("ignore", message=".*data discontinuity in recording.*")


class WindowsCapture(AudioCapture):
    """Captures the default playback device through WASAPI loopback."""

    def __init__(self, sample_rate: int = 48000, device_id: str | None = None) -> None:
        self.sample_rate = sample_rate
        self._device_id = device_id
        self._recorder = None
        self._channels = 2

    def open(self) -> None:
        import soundcard as sc

        speaker = sc.get_speaker(self._device_id) if self._device_id else sc.default_speaker()
        loopback = sc.get_microphone(speaker.id, include_loopback=True)
        self._channels = min(2, max(1, loopback.channels))
        self._recorder = loopback.recorder(
            samplerate=self.sample_rate, channels=self._channels
        )
        self._recorder.__enter__()

    def read(self, seconds: float) -> np.ndarray:
        frames = int(self.sample_rate * seconds)
        data = np.asarray(self._recorder.record(numframes=frames), dtype=np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1)  # downmix to mono
        return data

    def close(self) -> None:
        if self._recorder is not None:
            self._recorder.__exit__(None, None, None)
            self._recorder = None
