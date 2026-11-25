"""ListenBrainz scrobbling.

Needs a user token, which lives at
https://listenbrainz.org/settings/ under "User Token".

Both "now playing" and completed scrobbles go to the same endpoint,
``/1/submit-listens``; they are told apart by ``listen_type``
(``playing_now`` vs ``single``). There is no separate ``playing-now``
endpoint — posting to one returned HTTP 404.

Every failure is contained here: this module never raises into the
recognition loop, so a ListenBrainz problem cannot stop the app from
identifying music.
"""

from __future__ import annotations

import logging
import time

import requests

from .. import credentials

log = logging.getLogger(__name__)

API_ROOT = "https://api.listenbrainz.org/1/"
SUBMIT_PATH = "submit-listens"

# Never let a request hang the shutdown path: Stop/Quit must stay responsive.
DEFAULT_TIMEOUT = 10.0


class ListenBrainz:
    """Submits listens to ListenBrainz."""

    name = "ListenBrainz"

    def __init__(self, token: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.raw_token = token
        self.token = credentials.normalize_secret(token)
        self._problem = credentials.secret_problem(self.token)
        self.timeout = timeout
        self.last_error: str | None = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "ChocolateEggCream/1.0"})
        if self._problem:
            # Log the reason, never the value.
            log.warning(
                "ListenBrainz user token is not usable (%s). "
                "Paste the token again under Setup… -> ListenBrainz.",
                self._problem,
            )

    @property
    def configured(self) -> bool:
        return bool(self.token) and self._problem is None

    @property
    def problem(self) -> str | None:
        """Why the token cannot be used, or None when it is usable."""
        if not self.token:
            return "no token configured"
        return self._problem

    def _headers(self) -> dict[str, str]:
        # Only ever built from a validated, ASCII-only token: a non-Latin-1
        # value crashed `http.client.putheader` with UnicodeEncodeError.
        return {"Authorization": f"Token {self.token}"}

    @staticmethod
    def _metadata(artist: str, title: str, album: str | None, duration: float | None) -> dict:
        metadata: dict = {
            "artist_name": artist,
            "track_name": title,
        }
        if album:
            metadata["release_name"] = album
        if duration:
            metadata["additional_info"] = {
                "duration_ms": int(duration * 1000),
                "media_player": "Chocolate Egg Cream",
            }
        else:
            metadata["additional_info"] = {"media_player": "Chocolate Egg Cream"}
        return metadata

    def now_playing(self, artist: str, title: str, album: str | None = None,
                    duration: float | None = None) -> bool:
        """Tell ListenBrainz what is playing right now."""
        payload = {
            "listen_type": "playing_now",
            "payload": [{"track_metadata": self._metadata(artist, title, album, duration)}],
        }
        return self._post(SUBMIT_PATH, payload, what="now playing")

    def scrobble(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        timestamp: int | None = None,
        duration: float | None = None,
    ) -> bool:
        """Submit a finished play."""
        payload = {
            "listen_type": "single",
            "payload": [
                {
                    "listened_at": int(timestamp if timestamp is not None else time.time()),
                    "track_metadata": self._metadata(artist, title, album, duration),
                }
            ],
        }
        return self._post(SUBMIT_PATH, payload, what="scrobble")

    def _post(self, endpoint: str, payload: dict, what: str = "request") -> bool:
        if not self.configured:
            self.last_error = self.problem or "not configured"
            log.warning("ListenBrainz %s skipped: %s", what, self.last_error)
            return False

        url = f"{API_ROOT}{endpoint}"
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except (requests.RequestException, UnicodeEncodeError, ValueError) as exc:
            # ValueError/UnicodeEncodeError cover invalid header values, which
            # used to escape and take the whole recognition loop down.
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("ListenBrainz %s failed before it was sent: %s", what, self.last_error)
            return False

        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:200]
            self.last_error = f"HTTP {response.status_code}: {detail}"
            log.warning("ListenBrainz %s failed: %s", what, self.last_error)
            return False

        self.last_error = None
        return True

    def close(self) -> None:
        self._session.close()
