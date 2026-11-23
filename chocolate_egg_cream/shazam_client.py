"""Thin async wrapper around the ShazamIO client."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class ShazamClient:
    """Keeps one ShazamIO client alive and hides its API details."""

    def __init__(self, timeout: float = 45.0) -> None:
        self.timeout = timeout
        self._shazam = None

    async def _get(self):
        if self._shazam is None:
            from shazamio import Shazam

            self._shazam = Shazam()
        return self._shazam

    async def recognize(
        self,
        audio: bytes,
        segment_seconds: int | None = None,
        max_skew: float | None = None,
    ) -> dict | None:
        """Send audio to Shazam and return the raw response.

        Returns None when Shazam simply does not know the sample. Network
        problems and timeouts are logged and treated the same way, so a bad
        sample can never take the app down.
        """
        from shazamio_core import SearchParams

        shazam = await self._get()
        options = None
        if segment_seconds:
            options = SearchParams(segment_duration_seconds=segment_seconds)

        try:
            return await asyncio.wait_for(
                shazam.recognize(audio, options=options, max_skew=max_skew),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Shazam did not answer within %.0fs", self.timeout)
            return None
        except Exception as exc:  # noqa: BLE001 - recognition must never crash the app
            log.warning("Shazam request failed: %s", exc)
            return None

    async def close(self) -> None:
        if self._shazam is not None:
            try:
                await self._shazam.close()
            except Exception:  # noqa: BLE001
                pass
            self._shazam = None
