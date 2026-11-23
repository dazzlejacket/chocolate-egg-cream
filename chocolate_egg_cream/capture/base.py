"""The one interface every platform has to satisfy.

System audio only: this must never touch the microphone.
"""

from __future__ import annotations

import abc

import numpy as np


class AudioCapture(abc.ABC):
    """Captures what the computer is playing.

    `read()` always returns mono float32 samples in the range [-1.0, 1.0].
    """

    sample_rate: int = 48000

    @abc.abstractmethod
    def open(self) -> None:
        """Start capturing. Must be called before `read()`."""

    @abc.abstractmethod
    def read(self, seconds: float) -> np.ndarray:
        """Block until `seconds` of audio was captured, then return it."""

    @abc.abstractmethod
    def close(self) -> None:
        """Stop capturing and release the device."""

    def __enter__(self) -> AudioCapture:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
