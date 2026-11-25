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

# Scrobble de-duplication works on *playback events*, never on songs forever:
# the same service + song + start time is one event and is sent once, and a
# song played again after this many seconds counts as a new listen.
SCROBBLE_DEDUP_WINDOW = 600.0

# Persisted events older than this (or beyond the cap) are forgotten.
SCROBBLE_EVENT_MAX_AGE = 48 * 3600
SCROBBLE_EVENT_LIMIT = 200


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
        # One entry per submitted playback event, per service: (service,
        # song identity, when the track started). Seeded from settings so a
        # restart cannot send the same event twice, and pruned by age so a
        # song is never blocked forever.
        self._scrobble_events: list[dict] = self._load_events(settings)
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
            try:
                await self._finish(finished)
            except Exception as exc:  # noqa: BLE001 - Stop must always succeed
                log.warning("Finishing the track on Stop failed: %s", type(exc).__name__)
        self._now_playing_for = None
        self._last_now_playing_at = None
        self._heard_audio = None
        self._emit("stopped", None)

    async def shutdown(self) -> None:
        """Stop everything and release the network client."""
        await self.stop()
        try:
            await self.recognizer.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Closing the recognizer failed: %s", type(exc).__name__)
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
                    try:
                        recognition = await self.recognizer.recognize(
                            samples, self.capture.sample_rate
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - keep recognising
                        log.warning(
                            "Recognition failed (%s: %s); treating it as no match",
                            type(exc).__name__,
                            str(exc)[:160],
                        )

                finished = self.tracks.observe(recognition, time.time(), heard_audio=heard)

                if finished is not None:
                    try:
                        await self._finish(finished)
                    except Exception as exc:  # noqa: BLE001 - never stop the loop
                        log.warning(
                            "Submitting %s failed (%s: %s); recognition continues",
                            finished.recognition.label,
                            type(exc).__name__,
                            str(exc)[:160],
                        )

                current = self.tracks.current
                if current is not None:
                    due = self._now_playing_for != current.identity
                    stale = (
                        self._last_now_playing_at is not None
                        and time.time() - self._last_now_playing_at > NOW_PLAYING_REFRESH
                    )
                    if due or stale:
                        try:
                            await self._announce_now_playing()
                        except Exception as exc:  # noqa: BLE001 - never stop the loop
                            log.warning(
                                "Now playing update failed (%s: %s); recognition continues",
                                type(exc).__name__,
                                str(exc)[:160],
                            )

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
        """A confirmed track was replaced: decide whether to scrobble it.

        Every service is handled on its own: a failure (or an invalid
        credential) for one scrobbler is logged and skipped, and the other
        services plus the recognition loop keep running.
        """
        track = finished.recognition
        if not self._qualifies(finished):
            log.info(
                "Not scrobbling %s (played %ds)", track.label, int(finished.played_seconds)
            )
            return

        active = [scrobbler for scrobbler in self.scrobblers if scrobbler.configured]
        if not active:
            # Nothing to send to. The event must not be remembered, or it
            # would be skipped once a service is set up.
            log.info("No service is configured, not scrobbling %s", track.label)
            return

        timestamp = int(finished.started_at)
        now = time.time()
        submitted = False

        for scrobbler in active:
            if not self._may_submit(scrobbler.name, track.identity, timestamp, now):
                log.info(
                    "%s already has this listen of %s, skipping",
                    scrobbler.name,
                    track.label,
                )
                continue
            try:
                ok = await asyncio.to_thread(
                    scrobbler.scrobble,
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    timestamp=timestamp,
                    duration=track.duration,
                )
            except Exception as exc:  # noqa: BLE001 - one service must never stop the app
                log.warning(
                    "%s scrobble of %s raised %s: %s",
                    scrobbler.name,
                    track.label,
                    type(exc).__name__,
                    str(exc)[:160],
                )
                continue
            if ok:
                submitted = True
                self._record_submission(scrobbler.name, track.identity, timestamp, now)
                log.info("%s scrobbled %s", scrobbler.name, track.label)
            else:
                detail = getattr(scrobbler, "last_error", None)
                log.warning(
                    "%s failed to scrobble %s%s",
                    scrobbler.name,
                    track.label,
                    f" ({detail})" if detail else "",
                )

        if submitted:
            self._emit("scrobbled", track)

    # ---------------------------------------------------------- deduplication

    @staticmethod
    def _load_events(settings: dict) -> list[dict]:
        """Read persisted scrobble events, dropping anything malformed or old."""
        now = time.time()
        events: list[dict] = []
        raw = settings.get("scrobble_events") or settings.get("scrobbled_events") or []
        if not isinstance(raw, list):
            return events
        for item in raw:
            if not isinstance(item, dict):
                continue
            service = item.get("s") or item.get("service")
            identity = item.get("i") or item.get("identity")
            moment = item.get("t") or item.get("at")
            if not isinstance(service, str) or not isinstance(identity, str):
                continue
            if not isinstance(moment, (int, float)):
                continue
            if now - float(moment) >= SCROBBLE_EVENT_MAX_AGE:
                continue
            event = {"s": service, "i": identity, "t": int(moment)}
            started = item.get("started")
            if isinstance(started, (int, float)):
                event["started"] = int(started)
            events.append(event)
        return events[-SCROBBLE_EVENT_LIMIT:]

    def _may_submit(
        self, service: str, identity: str, started_at: float, now: float
    ) -> bool:
        """Has this service already been sent this exact playback event?"""
        wanted = int(started_at)
        for event in self._scrobble_events:
            if (
                event["s"] == service
                and event["i"] == identity
                and event.get("started") == wanted
            ):
                return False
            if (
                event["s"] == service
                and event["i"] == identity
                and now - event["t"] < SCROBBLE_DEDUP_WINDOW
            ):
                # The same song went to this service moments ago: the same
                # listen (a restart, Stop/Start, or a double finish), not a
                # new play.
                return False
        return True

    def _record_submission(
        self, service: str, identity: str, started_at: float, now: float
    ) -> None:
        """Remember that *this service* received this playback event."""
        self._scrobble_events.append(
            {"s": service, "i": identity, "t": int(now), "started": int(started_at)}
        )
        self._persist_scrobble_events()

    def _persist_scrobble_events(self) -> None:
        """Save the (bounded, age-pruned) event list without risking the loop."""
        now = time.time()
        self._scrobble_events = [
            event
            for event in self._scrobble_events
            if now - event["t"] < SCROBBLE_EVENT_MAX_AGE
        ][-SCROBBLE_EVENT_LIMIT:]
        self.settings["scrobble_events"] = [
            {"s": e["s"], "i": e["i"], "t": e["t"], "started": e.get("started", e["t"])}
            for e in self._scrobble_events
        ]
        # The legacy forever-list is gone; keep any old file key from coming back.
        self.settings.pop("scrobbled", None)
        try:
            self._save_settings(self.settings)
        except Exception as exc:  # noqa: BLE001 - a full disk must not stop recognition
            log.warning("Could not save scrobble history: %s", type(exc).__name__)

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
                ok = await asyncio.to_thread(
                    scrobbler.now_playing,
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    duration=track.duration,
                )
            except Exception as exc:  # noqa: BLE001
                detail = getattr(scrobbler, "last_error", None)
                log.warning(
                    "%s now playing update failed for %s (%s: %s%s)",
                    scrobbler.name,
                    track.label,
                    type(exc).__name__,
                    str(exc)[:120],
                    f" - {detail}" if detail else "",
                )
                continue
            if not ok:
                detail = getattr(scrobbler, "last_error", None)
                log.warning(
                    "%s rejected the now playing update for %s%s",
                    scrobbler.name,
                    track.label,
                    f" ({detail})" if detail else "",
                )
        self._emit("now_playing", track)

    def _emit(self, kind: str, payload) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass
