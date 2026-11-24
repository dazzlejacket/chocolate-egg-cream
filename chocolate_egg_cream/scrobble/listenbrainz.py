"""ListenBrainz scrobbling.

Needs a user token, which lives at
https://listenbrainz.org/settings/ under "User Token".
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.listenbrainz.org/1/"


class ListenBrainz:
    """Submits listens to ListenBrainz."""

    name = "ListenBrainz"

    def __init__(self, token: str, timeout: float = 15.0) -> None:
        self.token = token
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "ChocolateEggCream/1.0"})

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
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
            "track_metadata": self._metadata(artist, title, album, duration),
        }
        return self._post("playing-now", payload)

    def scrobble(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        timestamp: int | None = None,
        duration: float | None = None,
    ) -> bool:
        """Submit a finished play."""
        import time

        payload = {
            "listen_type": "single",
            "payload": [
                {
                    "listened_at": int(timestamp if timestamp is not None else time.time()),
                    "track_metadata": self._metadata(artist, title, album, duration),
                }
            ],
        }
        return self._post("submit-listens", payload)

    def _post(self, endpoint: str, payload: dict) -> bool:
        try:
            response = self._session.post(
                f"{API_ROOT}{endpoint}",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.warning("ListenBrainz request failed: %s", exc.__class__.__name__)
            return False

        if response.status_code >= 400:
            log.warning("ListenBrainz error %s: %s", response.status_code, response.text[:200])
            return False
        return True

    def close(self) -> None:
        self._session.close()
