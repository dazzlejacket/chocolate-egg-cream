"""Turn a sample of system audio into an artist and a title.

Recognition providers implement the `Recognizer` protocol so the backend
(ShazamIO by default) can be replaced cleanly if needed.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from .shazam_client import ShazamClient


@dataclass(frozen=True)
class Recognition:
    """One song that a recognition provider identified."""

    artist: str
    title: str
    album: str | None = None
    shazam_key: str | None = None
    isrc: str | None = None
    duration: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        return f"{self.artist} — {self.title}"

    @property
    def identity(self) -> str:
        """Stable identity of the song, used to spot repeats and duplicates."""
        if self.isrc:
            return f"isrc:{self.isrc}"
        return f"{self.artist.strip().lower()}::{self.title.strip().lower()}"


@runtime_checkable
class Recognizer(Protocol):
    """Minimal interface for a pluggable audio recognition provider."""

    name: str

    async def recognize(self, samples: np.ndarray, sample_rate: int) -> Recognition | None:
        """Return the song in `samples`, or None on no-match / transient error."""

    async def close(self) -> None:
        """Release any underlying network or native resources."""


def to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Wrap mono float32 samples in a WAV container in memory (never written to disk)."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def parse_response(response: dict) -> Recognition | None:
    """Pull the artist and title out of a Shazam response."""
    track = response.get("track") or {}
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return None

    album = None
    for section in track.get("sections") or []:
        for entry in section.get("metadata") or []:
            if (entry.get("title") or "").lower() == "album":
                album = (entry.get("text") or "").strip() or None

    return Recognition(
        artist=artist or "Unknown artist",
        title=title,
        album=album,
        shazam_key=track.get("key"),
        isrc=track.get("isrc"),
        raw=response,
    )


class ShazamRecognizer:
    """Recognizes samples of system audio with ShazamIO."""

    name = "Shazam"

    def __init__(
        self,
        segment_seconds: int = 10,
        max_skew: float | None = None,
        client: ShazamClient | None = None,
    ) -> None:
        self.segment_seconds = segment_seconds
        self.max_skew = max_skew
        self._client = client if client is not None else ShazamClient()

    @property
    def in_backoff(self) -> bool:
        return self._client.in_backoff

    @property
    def backoff_remaining(self) -> float:
        return self._client.backoff_remaining

    @property
    def last_error(self) -> str | None:
        return self._client.last_error

    async def recognize(self, samples: np.ndarray, sample_rate: int) -> Recognition | None:
        """Return the song in `samples`, or None when Shazam does not know it."""
        response = await self._client.recognize(
            to_wav_bytes(samples, sample_rate),
            segment_seconds=self.segment_seconds,
            max_skew=self.max_skew,
        )
        if not response:
            return None
        return parse_response(response)

    async def close(self) -> None:
        await self._client.close()


def create_recognizer(settings: dict) -> Recognizer:
    """Create the configured recognition provider (ShazamIO by default)."""
    provider = str(settings.get("recognition_provider", "shazam") or "shazam").strip().lower()
    segment_seconds = int(settings.get("sample_seconds", 12))
    max_skew_val = float(settings.get("max_skew", 0) or 0)
    max_skew = max_skew_val if max_skew_val > 0 else None

    if provider in ("shazam", "shazamio", ""):
        return ShazamRecognizer(
            segment_seconds=segment_seconds,
            max_skew=max_skew,
        )
    raise ValueError(
        f"Unsupported recognition provider {provider!r}. Supported: 'shazam'."
    )
