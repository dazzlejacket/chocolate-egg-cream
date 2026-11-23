"""Thin async wrapper around the ShazamIO client."""

from __future__ import annotations

import asyncio
import inspect
import logging

log = logging.getLogger(__name__)


class ShazamClient:
    """Keeps one ShazamIO client alive and hides its API details."""

    def __init__(self, timeout: float = 45.0) -> None:
        self.timeout = timeout
        self._shazam = None
        self._accepts_options: bool | None = None
        self._accepts_max_skew: bool | None = None

    async def _get(self):
        if self._shazam is None:
            from shazamio import Shazam

            self._shazam = Shazam()
            params = inspect.signature(self._shazam.recognize).parameters
            self._accepts_options = "options" in params
            self._accepts_max_skew = "max_skew" in params
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
        shazam = await self._get()
        kwargs: dict = {}
        if segment_seconds and self._accepts_options:
            try:
                from shazamio_core import SearchParams

                kwargs["options"] = SearchParams(segment_duration_seconds=segment_seconds)
            except ImportError:
                pass
        if max_skew is not None and self._accepts_max_skew:
            kwargs["max_skew"] = max_skew

        try:
            return await asyncio.wait_for(
                shazam.recognize(audio, **kwargs),
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
            close_fn = getattr(self._shazam, "close", None)
            if close_fn is not None:
                try:
                    await close_fn()
                except Exception:  # noqa: BLE001
                    pass
            self._shazam = None
