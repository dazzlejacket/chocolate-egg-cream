"""Decides what is playing right now.

Rules that matter:

* A recognition alone never counts. The same song has to come back twice
  before it becomes the current track.
* A failed recognition never ends the current track. The track stays until a
  different song is confirmed.
* Playtime only grows while audio was actually heard, so a stopped player
  does not make a track look long enough to scrobble.
"""

from __future__ import annotations

from dataclasses import dataclass

from .recognize import Recognition


@dataclass(frozen=True)
class FinishedTrack:
    """A track that stopped being the current one."""

    recognition: Recognition
    played_seconds: float
    started_at: float


class TrackState:
    """Turns a stream of recognitions into a confirmed current track."""

    def __init__(self, confirmations_required: int = 2) -> None:
        self.confirmations_required = max(2, confirmations_required)
        self.current: Recognition | None = None
        self.current_started_at: float | None = None
        self._played_seconds = 0.0
        self._last_update: float | None = None
        self._candidate: Recognition | None = None
        self._candidate_count = 0

    @property
    def playtime(self) -> float:
        """How long the current track has been playing, in seconds."""
        return self._played_seconds

    @property
    def candidate(self) -> Recognition | None:
        """The song that has been heard once and still needs a second look."""
        return self._candidate

    def observe(
        self,
        recognition: Recognition | None,
        now: float,
        heard_audio: bool = True,
    ) -> FinishedTrack | None:
        """Feed in one recognition result.

        Returns a FinishedTrack when a confirmed track was just replaced.
        """
        self._advance_playtime(now, heard_audio)

        if recognition is None:
            # No idea what this is. Keep the current track; a failure is not
            # evidence that the song ended.
            return None

        if self.current is not None and recognition.identity == self.current.identity:
            # Same song as the confirmed track: nothing changes.
            self._candidate = None
            self._candidate_count = 0
            return None

        if self._candidate is not None and recognition.identity == self._candidate.identity:
            self._candidate_count += 1
        else:
            self._candidate = recognition
            self._candidate_count = 1

        if self._candidate_count < self.confirmations_required:
            return None

        # The candidate is confirmed: it becomes the current track and the
        # previous one is over.
        previous = self.current
        finished = None
        if previous is not None:
            finished = FinishedTrack(
                recognition=previous,
                played_seconds=self._played_seconds,
                started_at=self.current_started_at or now,
            )

        self.current = recognition
        self.current_started_at = now
        self._played_seconds = 0.0
        self._last_update = now
        self._candidate = None
        self._candidate_count = 0
        return finished

    def _advance_playtime(self, now: float, heard_audio: bool) -> None:
        if self._last_update is not None and self.current is not None:
            delta = now - self._last_update
            if heard_audio and delta > 0:
                self._played_seconds += delta
        self._last_update = now

    def finish_current(self, now: float) -> FinishedTrack | None:
        """Treat the current track as over, for example when the app stops.

        Returns None when nothing was playing.
        """
        if self.current is None:
            return None

        finished = FinishedTrack(
            recognition=self.current,
            played_seconds=self._played_seconds,
            started_at=self.current_started_at or now,
        )
        self.current = None
        self.current_started_at = None
        self._played_seconds = 0.0
        self._candidate = None
        self._candidate_count = 0
        return finished
