"""Thin async wrapper around the ShazamIO client with bounded retries and backoff."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)


class RecognitionError(RuntimeError):
    """Raised internally when the recognition backend is unreachable or rate-limited."""

    def __init__(self, message: str, *, retry_after: float | None = None, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


def _extract_http_status(exc: BaseException) -> tuple[int | None, float | None]:
    """Extract HTTP status code and optional Retry-After header from aiohttp/requests errors."""
    status = getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    retry_after: float | None = None
    headers = getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
    if headers:
        raw_ra = headers.get("Retry-After") or headers.get("retry-after")
        if raw_ra is not None:
            try:
                retry_after = max(0.0, float(raw_ra))
            except (TypeError, ValueError):
                retry_after = None
    return (int(status) if isinstance(status, int) else None, retry_after)


class ShazamClient:
    """Keeps one ShazamIO client alive with bounded retries and outage backoff."""

    def __init__(
        self,
        timeout: float = 45.0,
        max_retries: int = 1,
        retry_delay: float = 1.5,
        base_backoff: float = 15.0,
        max_backoff: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_delay = max(0.0, retry_delay)
        self.base_backoff = max(1.0, base_backoff)
        self.max_backoff = max(self.base_backoff, max_backoff)
        self._clock = clock or time.monotonic

        self._shazam = None
        self._accepts_options: bool | None = None
        self._accepts_max_skew: bool | None = None

        self.consecutive_failures: int = 0
        self.backoff_until: float = 0.0
        self.last_error: str | None = None

    @property
    def in_backoff(self) -> bool:
        return self._clock() < self.backoff_until

    @property
    def backoff_remaining(self) -> float:
        return max(0.0, self.backoff_until - self._clock())

    async def _get(self):
        if self._shazam is None:
            from shazamio import Shazam

            self._shazam = Shazam()
            params = inspect.signature(self._shazam.recognize).parameters
            self._accepts_options = "options" in params
            self._accepts_max_skew = "max_skew" in params
        return self._shazam

    def _record_success(self) -> None:
        if self.consecutive_failures > 0:
            log.info(
                "Shazam recognition recovered after %d consecutive failure(s)",
                self.consecutive_failures,
            )
        self.consecutive_failures = 0
        self.backoff_until = 0.0
        self.last_error = None

    def _record_failure(self, message: str, retry_after: float | None = None) -> None:
        self.consecutive_failures += 1
        self.last_error = message
        delay = min(self.max_backoff, self.base_backoff * (2 ** (self.consecutive_failures - 1)))
        if retry_after is not None:
            delay = min(self.max_backoff, max(delay, retry_after))
        self.backoff_until = self._clock() + delay
        log.warning(
            "Shazam unavailable (%s); backing off %.0fs (failure #%d)",
            message,
            delay,
            self.consecutive_failures,
        )

    async def recognize(
        self,
        audio: bytes,
        segment_seconds: int | None = None,
        max_skew: float | None = None,
    ) -> dict | None:
        """Send audio to Shazam and return the raw response.

        Returns None when Shazam does not know the sample or is currently in
        outage backoff. Network failures, timeouts, and rate limits trigger
        bounded retries and exponential backoff with automatic recovery.
        """
        if self.in_backoff:
            log.debug(
                "Skipping Shazam request during backoff (%.0fs remaining)",
                self.backoff_remaining,
            )
            return None

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

        attempts = 1 + self.max_retries
        last_exc_msg = ""
        last_retry_after: float | None = None

        for attempt in range(attempts):
            try:
                response = await asyncio.wait_for(
                    shazam.recognize(audio, **kwargs),
                    timeout=self.timeout,
                )
                self._record_success()
                return response
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                last_exc_msg = f"timed out after {self.timeout:.0f}s"
                last_retry_after = None
            except Exception as exc:  # noqa: BLE001 - recognition must never crash the app
                status, retry_after = _extract_http_status(exc)
                last_retry_after = retry_after
                if status == 429:
                    last_exc_msg = "rate limited (HTTP 429)"
                    # Do not immediately retry when rate-limited; back off right away.
                    break
                if status is not None and status >= 500:
                    last_exc_msg = f"server error (HTTP {status})"
                else:
                    last_exc_msg = str(exc) or exc.__class__.__name__

            if attempt + 1 < attempts and self.retry_delay > 0:
                log.debug(
                    "Shazam attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    last_exc_msg,
                    self.retry_delay,
                )
                await asyncio.sleep(self.retry_delay)

        self._record_failure(last_exc_msg, retry_after=last_retry_after)
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
