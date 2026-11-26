"""The engine: capture, recognise, confirm, record locally, scrobble."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from typing import Callable

import numpy as np

from .capture import get_capture
from .recognize import Recognition, create_recognizer
from .scrobble import external as external_module
from .scrobble import lastfm_app
from .scrobble.lastfm import LastFM
from .scrobble.listenbrainz import ListenBrainz
from .track import FinishedTrack, TrackState
from . import history as history_module
from . import settings as settings_module

log = logging.getLogger(__name__)

# Below this the sample counts as silence and is not worth sending to Shazam.
SILENCE_RMS = 0.002

# How often the "now playing" status is refreshed for a long track.
NOW_PLAYING_REFRESH = 300

# How often failed scrobbles in pending_scrobbles.json are retried while running.
PENDING_RETRY_INTERVAL = 300

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


def _scrobbler_service_key(scrobbler) -> str:
    key = getattr(scrobbler, "service_key", None)
    if isinstance(key, str) and key:
        return key
    name = str(getattr(scrobbler, "name", "service")).lower().replace(".", "")
    return name


def _call_scrobbler_method(fn, **kwargs) -> bool:
    """Call `scrobble`/`now_playing`, passing `isrc` only when accepted."""
    try:
        sig = inspect.signature(fn)
        if "isrc" not in sig.parameters and not any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            kwargs.pop("isrc", None)
    except (TypeError, ValueError):
        kwargs.pop("isrc", None)
    return bool(fn(**kwargs))


class EggCream:
    """Runs the recognise-and-scrobble loop."""

    def __init__(
        self,
        settings: dict,
        capture=None,
        recognizer=None,
        save_settings=None,
        hwnd_getter: Callable[[], int | None] | None = None,
        ui_dispatch: Callable[[Callable[[], None]], None] | None = None,
        record_history: bool = True,
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
        self._hwnd_getter = hwnd_getter
        self._ui_dispatch = ui_dispatch
        self._record_history = record_history
        self.tracks = TrackState(confirmations_required=int(settings["confirmations_required"]))
        self.scrobblers = self._build_scrobblers(settings)
        self.external_session: external_module.ExternalMediaSession | None = (
            self._build_external_session(settings)
        )
        # One entry per submitted playback event, per service: (service,
        # song identity, when the track started). Seeded from settings so a
        # restart cannot send the same event twice, and pruned by age so a
        # song is never blocked forever.
        self._scrobble_events: list[dict] = self._load_events(settings)
        self._recorded_listens: set[tuple[str, int]] = set()
        self._running = False
        self._task: asyncio.Task | None = None
        self._now_playing_for: str | None = None
        self._last_now_playing_at: float | None = None
        self._last_retry_at: float = 0.0
        self._heard_audio: bool | None = None
        self.history: list[Recognition] = []
        self.on_event = None  # optional callback for the display

    # ---------------------------------------------------------------- setup

    @staticmethod
    def _build_scrobblers(settings: dict) -> list:
        enabled = settings.get("enabled_services", [])
        scrobblers = []
        if "lastfm" in enabled:
            api_key, api_secret, _using_custom = lastfm_app.resolve_credentials(settings)
            scrobblers.append(
                LastFM(
                    api_key=api_key,
                    api_secret=api_secret,
                    session_key=settings.get("lastfm_session_key", ""),
                    username=settings.get("lastfm_username", ""),
                )
            )
        if "listenbrainz" in enabled:
            scrobblers.append(
                ListenBrainz(
                    token=settings.get("listenbrainz_token", ""),
                    server_url=settings.get("listenbrainz_url", ""),
                )
            )
        return scrobblers

    def _build_external_session(
        self, settings: dict
    ) -> external_module.ExternalMediaSession | None:
        if not settings.get("external_scrobbler"):
            return None
        supported, _reason = external_module.platform_support()
        if not supported:
            return None
        return external_module.create_external_session(
            hwnd_getter=self._hwnd_getter,
            ui_dispatch=self._ui_dispatch,
        )

    def reload_settings(self, settings: dict) -> None:
        """Apply updated settings (e.g. from Setup) without restarting the app."""
        self.settings = dict(settings)
        self._last_retry_at = 0.0
        old_scrobblers = self.scrobblers
        self.scrobblers = self._build_scrobblers(self.settings)
        for old in old_scrobblers:
            close_fn = getattr(old, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:  # noqa: BLE001
                    pass

        want_external = bool(self.settings.get("external_scrobbler"))
        if not want_external and self.external_session is not None:
            try:
                self.external_session.close()
            except Exception:  # noqa: BLE001
                pass
            self.external_session = None
        elif want_external and self.external_session is None:
            self.external_session = self._build_external_session(self.settings)
            if self._running and self.tracks.current is not None:
                self._now_playing_for = None

        max_skew = float(self.settings.get("max_skew", 0) or 0)
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
        if self.external_session is not None:
            try:
                self.external_session.clear()
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
        if self.external_session is not None:
            try:
                self.external_session.close()
            except Exception:  # noqa: BLE001
                pass
            self.external_session = None
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

                # Gently retry any previously failed scrobbles in the background.
                if (
                    self._record_history
                    and time.time() - self._last_retry_at >= PENDING_RETRY_INTERVAL
                ):
                    self._last_retry_at = time.time()
                    try:
                        await self.retry_pending()
                    except Exception:  # noqa: BLE001
                        pass

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
                elif self._now_playing_for is not None:
                    self._now_playing_for = None
                    self._last_now_playing_at = None
                    if self.external_session is not None and self.external_session.active:
                        try:
                            await asyncio.to_thread(self.external_session.clear)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("External media session clear failed: %s", exc)

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
        """A confirmed track was replaced: save locally and scrobble it.

        Every eligible completed listen is automatically appended to local
        history even when no scrobbling accounts are configured. Every
        service is handled independently, and failed scrobbles are preserved
        in `pending_scrobbles.json` for later retry.
        """
        track = finished.recognition
        if not self._qualifies(finished):
            log.info(
                "Not scrobbling %s (played %ds)", track.label, int(finished.played_seconds)
            )
            return

        timestamp = int(finished.started_at)
        now = time.time()
        listen_id = uuid.uuid4().hex
        service_statuses: dict[str, dict] = {}
        submitted = False

        active = [scrobbler for scrobbler in self.scrobblers if scrobbler.configured]

        for scrobbler in active:
            svc_key = _scrobbler_service_key(scrobbler)
            if not self._may_submit(scrobbler.name, track.identity, timestamp, now):
                log.info(
                    "%s already has this listen of %s, skipping",
                    scrobbler.name,
                    track.label,
                )
                service_statuses[svc_key] = {
                    "status": "skipped_duplicate",
                    "error": None,
                    "at": int(now),
                }
                continue
            try:
                ok = await asyncio.to_thread(
                    _call_scrobbler_method,
                    scrobbler.scrobble,
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    timestamp=timestamp,
                    duration=track.duration,
                    isrc=track.isrc,
                )
            except Exception as exc:  # noqa: BLE001 - one service must never stop the app
                detail = f"{type(exc).__name__}: {str(exc)[:160]}"
                log.warning(
                    "%s scrobble of %s raised %s",
                    scrobbler.name,
                    track.label,
                    detail,
                )
                service_statuses[svc_key] = {
                    "status": "failed",
                    "error": detail,
                    "at": int(now),
                }
                if self._record_history:
                    history_module.enqueue_pending(
                        listen_id=listen_id,
                        service=svc_key,
                        artist=track.artist,
                        title=track.title,
                        album=track.album,
                        timestamp=timestamp,
                        duration=track.duration,
                        isrc=track.isrc,
                        identity=track.identity,
                        error=detail,
                    )
                continue

            if ok:
                submitted = True
                self._record_submission(scrobbler.name, track.identity, timestamp, now)
                service_statuses[svc_key] = {
                    "status": "submitted",
                    "error": None,
                    "at": int(now),
                }
                log.info("%s scrobbled %s", scrobbler.name, track.label)
            else:
                detail = getattr(scrobbler, "last_error", None)
                log.warning(
                    "%s failed to scrobble %s%s",
                    scrobbler.name,
                    track.label,
                    f" ({detail})" if detail else "",
                )
                service_statuses[svc_key] = {
                    "status": "failed",
                    "error": detail,
                    "at": int(now),
                }
                if self._record_history:
                    history_module.enqueue_pending(
                        listen_id=listen_id,
                        service=svc_key,
                        artist=track.artist,
                        title=track.title,
                        album=track.album,
                        timestamp=timestamp,
                        duration=track.duration,
                        isrc=track.isrc,
                        identity=track.identity,
                        error=detail,
                    )
                if svc_key == "lastfm" and getattr(scrobbler, "session_stale", False):
                    self._emit("lastfm_stale", detail or "Last.fm session expired")

        if self.external_session is not None and self.external_session.active:
            service_statuses["external"] = {
                "status": "published",
                "error": None,
                "at": int(now),
            }

        # Automatically save every eligible completed listen locally, even when
        # no scrobbling services are configured.
        if self._record_history:
            event_key = (track.identity, timestamp)
            if event_key not in self._recorded_listens:
                self._recorded_listens.add(event_key)
                if len(self._recorded_listens) > 500:
                    self._recorded_listens.clear()
                try:
                    record = history_module.append_listen(
                        listen_id=listen_id,
                        artist=track.artist,
                        title=track.title,
                        album=track.album,
                        timestamp=timestamp,
                        duration=track.duration,
                        isrc=track.isrc,
                        identity=track.identity,
                        services=service_statuses,
                    )
                    self._emit("listen_saved", record)
                except OSError as exc:
                    log.warning("Could not append local history: %s", exc)

        if not active:
            log.info(
                "No scrobble service configured; saved %s to local history only",
                track.label,
            )

        if submitted:
            self._emit("scrobbled", track)

    async def retry_pending(self) -> tuple[int, int]:
        """Retry previously failed scrobbles. Returns `(succeeded, remaining)`."""
        pending = history_module.load_pending()
        if not pending:
            return 0, 0

        by_key = {
            _scrobbler_service_key(s): s
            for s in self.scrobblers
            if s.configured
        }
        if not by_key:
            return 0, len(pending)

        remaining: list[dict] = []
        succeeded = 0
        now = time.time()

        for item in pending:
            svc_key = str(item.get("service") or "")
            scrobbler = by_key.get(svc_key)
            if scrobbler is None or getattr(scrobbler, "session_stale", False):
                remaining.append(item)
                continue

            identity = str(item.get("identity") or "")
            timestamp = int(item.get("timestamp") or 0)
            if not self._may_submit(scrobbler.name, identity, timestamp, now):
                # Already submitted previously; drop to prevent duplicates.
                continue

            try:
                ok = await asyncio.to_thread(
                    _call_scrobbler_method,
                    scrobbler.scrobble,
                    artist=str(item.get("artist") or ""),
                    title=str(item.get("title") or ""),
                    album=item.get("album"),
                    timestamp=timestamp,
                    duration=item.get("duration"),
                    isrc=item.get("isrc"),
                )
            except Exception as exc:  # noqa: BLE001
                item["attempts"] = int(item.get("attempts") or 1) + 1
                item["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                item["updated_at"] = int(time.time())
                remaining.append(item)
                continue

            if ok:
                succeeded += 1
                self._record_submission(scrobbler.name, identity, timestamp, now)
                listen_id = str(item.get("listen_id") or "")
                if listen_id:
                    try:
                        history_module.append_status_update(
                            listen_id, svc_key, "submitted", at=now
                        )
                    except OSError:
                        pass
                log.info(
                    "Retry succeeded: %s scrobbled %s — %s",
                    scrobbler.name,
                    item.get("artist"),
                    item.get("title"),
                )
            else:
                detail = getattr(scrobbler, "last_error", None)
                item["attempts"] = int(item.get("attempts") or 1) + 1
                item["last_error"] = detail
                item["updated_at"] = int(time.time())
                remaining.append(item)
                if svc_key == "lastfm" and getattr(scrobbler, "session_stale", False):
                    self._emit("lastfm_stale", detail or "Last.fm session expired")

        history_module.save_pending(remaining)
        if succeeded:
            self._emit("retried", {"succeeded": succeeded, "remaining": len(remaining)})
        return succeeded, len(remaining)

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

        if self.external_session is not None and (is_new or not self.external_session.active):
            try:
                ok_ext = await asyncio.to_thread(
                    self.external_session.update_now_playing,
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    duration=track.duration,
                    track_id=track.identity,
                    position=max(0.0, float(self.tracks.playtime)),
                )
                if not ok_ext and self.external_session.last_error:
                    log.warning(
                        "%s update failed: %s",
                        self.external_session.backend_name,
                        self.external_session.last_error,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("External media session update failed: %s", exc)

        for scrobbler in self.scrobblers:
            if not scrobbler.configured:
                continue
            try:
                ok = await asyncio.to_thread(
                    _call_scrobbler_method,
                    scrobbler.now_playing,
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    duration=track.duration,
                    isrc=track.isrc,
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
                if _scrobbler_service_key(scrobbler) == "lastfm" and getattr(
                    scrobbler, "session_stale", False
                ):
                    self._emit("lastfm_stale", detail or "Last.fm session expired")
        self._emit("now_playing", track)

    def _emit(self, kind: str, payload) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass
