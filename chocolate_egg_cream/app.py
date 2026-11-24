"""The engine: capture, recognise, confirm, scrobble."""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from .capture import get_capture
from .recognize import Recognition, create_recognizer
from .scrobble.listenbrainz import ListenBrainz
from .scrobble.lastfm import LastFM
from .track import FinishedTrack, TrackState
from . import settings as settings_module

log = logging.getLogger(__name__)

# Below this the sample counts as silence and is not worth sending to Shazam.
SILENCE_RMS = 0.002

# How often the "now playing" status is refreshed for a long track.
NOW_PLAYING_REFRESH = 300


def is_silent(samples: np.ndarray) -> bool:
    if samples.size == 0:
        return True
    return float(np.sqrt(np.mean(np.square(samples)))) < SILENCE_RMS


class EggCream:
    """Runs the recognise-and-scrobble loop."""

    def __init__(
        self,
        settings: dict,
        capture=None,
        recognizer=None,
        save_settings=None,
    ) -> None:
        self.settings = settings
        self.capture = (
            capture
            if capture is not None
            else get_capture(
                sample_rate=48000,
                macos_backend=str(settings.get("macos_capture_backend", "auto") or "auto"),
            )
        )
        self.recognizer = (
            recognizer
            if recognizer is not None
            else create_recognizer(settings)
        )
        self._save_settings = save_settings or settings_module.save
        self.tracks = TrackState(confirmations_required=int(settings["confirmations_required"]))
        self.scrobblers = self._build_scrobblers(settings)
        # Keep insertion order so trimming to the last 500 drops the oldest songs.
        self._scrobbled: dict[str, None] = dict.fromkeys(settings.get("scrobbled", []))
        self._running = False
        self._task: asyncio.Task | None = None
        self._now_playing_for: str | None = None
        self._last_now_playing_at: float | None = None
        self._heard_audio: bool | None = None
        self.history: list[Recognition] = []
        self.on_event = None  # optional callback for the display

    # ---------------------------------------------------------------- setup

    @staticmethod
    def _build_scrobblers(settings: dict) -> list:
        enabled = settings.get("enabled_services", [])
        scrobblers = []
        if "lastfm" in enabled:
            scrobblers.append(
                LastFM(
                    api_key=settings.get("lastfm_api_key", ""),
                    api_secret=settings.get("lastfm_api_secret", ""),
                    session_key=settings.get("lastfm_session_key", ""),
                )
            )
        if "listenbrainz" in enabled:
            scrobblers.append(
                ListenBrainz(token=settings.get("listenbrainz_token", ""))
            )
        return scrobblers

    def reload_settings(self, settings: dict) -> None:
        """Apply updated settings (e.g. from Setup) without restarting the app."""
        self.settings = settings
        self.scrobblers = self._build_scrobblers(settings)
        max_skew = float(settings.get("max_skew", 0) or 0)
        if hasattr(self.recognizer, "max_skew"):
            self.recognizer.max_skew = max_skew if max_skew > 0 else None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def current(self) -> Recognition | None:
        return self.tracks.current

    @property
    def candidate(self) -> Recognition | None:
        return self.tracks.candidate

    @property
    def playtime(self) -> float:
        return self.tracks.playtime

    @property
    def heard_audio(self) -> bool | None:
        """Whether the most recent sample had audio (None before first sample)."""
        return self._heard_audio

    @property
    def recognizer_backoff(self) -> float:
        """Seconds remaining in recognition outage backoff (0.0 when healthy)."""
        if getattr(self.recognizer, "in_backoff", False):
            return float(getattr(self.recognizer, "backoff_remaining", 0.0))
        return 0.0

    # ------------------------------------------------------------- control

    async def start(self) -> None:
        """Start capturing and recognising."""
        if self._running:
            return
        try:
            await asyncio.to_thread(self.capture.open)
        except Exception as exc:
            self._running = False
            self._emit("error", str(exc))
            raise
        self._heard_audio = None
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._emit("started", None)

    async def stop(self) -> None:
        """Stop the loop and release the audio device."""
        self._running = False
        request_stop = getattr(self.capture, "request_stop", None)
        if callable(request_stop):
            try:
                request_stop()
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        try:
            await asyncio.to_thread(self.capture.close)
        except Exception:  # noqa: BLE001
            pass
        # The song that was playing is over now.
        finished = self.tracks.finish_current(time.time())
        if finished is not None:
            await self._finish(finished)
        self._now_playing_for = None
        self._last_now_playing_at = None
        self._heard_audio = None
        self._emit("stopped", None)

    async def shutdown(self) -> None:
        """Stop everything and release the network client."""
        await self.stop()
        await self.recognizer.close()
        for scrobbler in self.scrobblers:
            close_fn = getattr(scrobbler, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- loop

    async def _loop(self) -> None:
        interval = float(self.settings["interval_seconds"])
        sample_seconds = float(self.settings["sample_seconds"])
        consecutive_capture_errors = 0
        try:
            while self._running:
                started = time.time()
                try:
                    samples = await asyncio.to_thread(self.capture.read, sample_seconds)
                    consecutive_capture_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    consecutive_capture_errors += 1
                    log.warning("Audio capture failed: %s", exc)
                    if consecutive_capture_errors >= 3:
                        self._running = False
                        self._emit("error", str(exc))
                        return
                    await asyncio.sleep(2)
                    continue

                heard = not is_silent(samples)
                self._heard_audio = heard
                recognition = None
                if heard:
                    recognition = await self.recognizer.recognize(
                        samples, self.capture.sample_rate
                    )

                finished = self.tracks.observe(recognition, time.time(), heard_audio=heard)

                if finished is not None:
                    await self._finish(finished)

                current = self.tracks.current
                if current is not None:
                    due = self._now_playing_for != current.identity
                    stale = (
                        self._last_now_playing_at is not None
                        and time.time() - self._last_now_playing_at > NOW_PLAYING_REFRESH
                    )
                    if due or stale:
                        await self._announce_now_playing()

                elapsed = time.time() - started
                await asyncio.sleep(max(0.0, interval - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("The recognition loop stopped unexpectedly")
            self._running = False
            self._emit("error", str(exc))

    # ------------------------------------------------------------- helpers

    async def _finish(self, finished: FinishedTrack) -> None:
        """A confirmed track was replaced: decide whether to scrobble it."""
        track = finished.recognition
        if not self._qualifies(finished):
            log.info(
                "Not scrobbling %s (played %ds)", track.label, int(finished.played_seconds)
            )
            return

        identity = track.identity
        if identity in self._scrobbled:
            log.info("Already scrobbled %s, skipping", track.label)
            return

        active = [scrobbler for scrobbler in self.scrobblers if scrobbler.configured]
        if not active:
            # Nothing to send to. The song must not be marked as scrobbled,
            # or it would be skipped once a service is set up.
            log.info("No service is configured, not scrobbling %s", track.label)
            return

        submitted = False
        timestamp = int(finished.started_at)
        for scrobbler in active:
            ok = await asyncio.to_thread(
                scrobbler.scrobble,
                artist=track.artist,
                title=track.title,
                album=track.album,
                timestamp=timestamp,
                duration=track.duration,
            )
            if ok:
                submitted = True
            log.info(
                "%s %s %s",
                scrobbler.name,
                "scrobbled" if ok else "failed to scrobble",
                track.label,
            )

        if submitted:
            self._scrobbled[identity] = None
            self._remember_scrobbled()
            self._emit("scrobbled", track)

    def _qualifies(self, finished: FinishedTrack) -> bool:
        """Did the track play long enough to count as a real listen?"""
        played = finished.played_seconds
        duration = finished.recognition.duration
        if duration:
            return played >= 30 and played >= min(duration * 0.5, 240)
        return played >= float(self.settings["min_play_seconds"])

    async def _announce_now_playing(self) -> None:
        track = self.tracks.current
        if track is None:
            return
        is_new = self._now_playing_for != track.identity
        self._now_playing_for = track.identity
        self._last_now_playing_at = time.time()
        if is_new:
            if not self.history or self.history[0].identity != track.identity:
                self.history.insert(0, track)
                del self.history[25:]
        log.info("Now playing: %s", track.label)
        for scrobbler in self.scrobblers:
            if not scrobbler.configured:
                continue
            try:
                await asyncio.to_thread(
                    scrobbler.now_playing,
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    duration=track.duration,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s now playing update failed: %s", scrobbler.name, exc)
        self._emit("now_playing", track)

    def _remember_scrobbled(self) -> None:
        recent = list(self._scrobbled.keys())[-500:]
        self._scrobbled = dict.fromkeys(recent)
        self.settings["scrobbled"] = recent
        self._save_settings(self.settings)

    def _emit(self, kind: str, payload) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass
