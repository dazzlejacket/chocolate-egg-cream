"""ListenBrainz (and compatible server / Multi-Scrobbler) scrobbling.

Needs a user token, which lives at https://listenbrainz.org/settings/ under
"User Token", or the token configured on your custom ListenBrainz-compatible
endpoint (such as Multi-Scrobbler or Maloja).

Both "now playing" and completed scrobbles go to ``/1/submit-listens``; they
are told apart by ``listen_type`` (``playing_now`` vs ``single``).

Every failure is contained here: this module never raises into the
recognition loop, so a ListenBrainz problem cannot stop the app from
identifying music.
"""

from __future__ import annotations

import logging
import time
import urllib.parse

import requests

from .. import __version__, credentials

log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "https://api.listenbrainz.org"
API_ROOT = "https://api.listenbrainz.org/1/"
SUBMIT_PATH = "submit-listens"

# Never let a request hang the shutdown path: Stop/Quit must stay responsive.
DEFAULT_TIMEOUT = 10.0


def normalize_submit_url(server_url: str | None) -> tuple[str, str | None]:
    """Normalize a ListenBrainz or Multi-Scrobbler base URL to `/1/submit-listens`.

    Returns `(full_submit_url, problem_or_none)`.
    Accepts:
      - "" / None                               -> https://api.listenbrainz.org/1/submit-listens
      - "https://api.listenbrainz.org"          -> https://api.listenbrainz.org/1/submit-listens
      - "https://api.listenbrainz.org/1/"       -> https://api.listenbrainz.org/1/submit-listens
      - "http://localhost:9078"                 -> http://localhost:9078/1/submit-listens
      - "http://localhost:9078/1/submit-listens"-> http://localhost:9078/1/submit-listens
    """
    raw = (server_url or "").strip()
    if not raw:
        return f"{API_ROOT}{SUBMIT_PATH}", None

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "", "server URL must start with http:// or https:// and include a host"

    cleaned = raw.rstrip("/")
    if cleaned.endswith(f"/1/{SUBMIT_PATH}"):
        return cleaned, None
    if cleaned.endswith("/1"):
        return f"{cleaned}/{SUBMIT_PATH}", None
    return f"{cleaned}/1/{SUBMIT_PATH}", None


class ListenBrainz:
    """Submits listens to ListenBrainz or a compatible server."""

    name = "ListenBrainz"
    service_key = "listenbrainz"

    def __init__(
        self,
        token: str,
        server_url: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.raw_token = token
        self.token = credentials.normalize_secret(token)
        self.server_url = (server_url or "").strip()
        self.submit_url, self._url_problem = normalize_submit_url(self.server_url)
        self._problem = credentials.secret_problem(self.token)
        self.timeout = timeout
        self.last_error: str | None = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": f"ChocolateEggCream/{__version__}"})
        if self._problem:
            # Log the reason, never the value.
            log.warning(
                "ListenBrainz user token is not usable (%s). "
                "Paste the token again under Setup… -> ListenBrainz.",
                self._problem,
            )
        if self._url_problem:
            log.warning("ListenBrainz server URL is not usable: %s", self._url_problem)

    @property
    def configured(self) -> bool:
        return bool(self.token) and self._problem is None and self._url_problem is None

    @property
    def problem(self) -> str | None:
        """Why ListenBrainz cannot be used, or None when it is usable."""
        if not self.token:
            return "no token configured"
        if self._problem:
            return self._problem
        if self._url_problem:
            return self._url_problem
        return None

    def _headers(self) -> dict[str, str]:
        # Only ever built from a validated, ASCII-only token: a non-Latin-1
        # value crashed `http.client.putheader` with UnicodeEncodeError.
        return {"Authorization": f"Token {self.token}"}

    @staticmethod
    def _metadata(
        artist: str,
        title: str,
        album: str | None,
        duration: float | None,
        isrc: str | None = None,
    ) -> dict:
        metadata: dict = {
            "artist_name": artist,
            "track_name": title,
        }
        if album:
            metadata["release_name"] = album
        additional: dict = {"media_player": "Chocolate Egg Cream"}
        if duration:
            additional["duration_ms"] = int(duration * 1000)
        if isrc:
            additional["isrc"] = isrc
        metadata["additional_info"] = additional
        return metadata

    def now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
        isrc: str | None = None,
    ) -> bool:
        """Tell ListenBrainz what is playing right now."""
        payload = {
            "listen_type": "playing_now",
            "payload": [
                {"track_metadata": self._metadata(artist, title, album, duration, isrc)}
            ],
        }
        return self._post(SUBMIT_PATH, payload, what="now playing")

    def scrobble(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        timestamp: int | None = None,
        duration: float | None = None,
        isrc: str | None = None,
    ) -> bool:
        """Submit a finished play."""
        payload = {
            "listen_type": "single",
            "payload": [
                {
                    "listened_at": int(timestamp if timestamp is not None else time.time()),
                    "track_metadata": self._metadata(artist, title, album, duration, isrc),
                }
            ],
        }
        return self._post(SUBMIT_PATH, payload, what="scrobble")

    def _post(self, endpoint: str, payload: dict, what: str = "request") -> bool:
        if not self.configured:
            self.last_error = self.problem or "not configured"
            log.warning("ListenBrainz %s skipped: %s", what, self.last_error)
            return False

        # Respect module-level API_ROOT monkeypatches in tests when using default URL.
        if not self.server_url:
            url = f"{API_ROOT}{endpoint}"
        else:
            url = self.submit_url

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

    def verify_token(self) -> tuple[bool, str | None, str | None]:
        """Verify the ListenBrainz user token against `/1/validate-token`.

        Returns `(ok, username, error_message)`.
        """
        if not self.configured:
            err = self.problem or "not configured"
            self.last_error = err
            return False, None, err

        base = self.submit_url
        if base.endswith(f"/{SUBMIT_PATH}"):
            url = base[: -len(SUBMIT_PATH)] + "validate-token"
        else:
            url = f"{API_ROOT}validate-token"

        try:
            response = self._session.get(
                url, headers=self._headers(), timeout=self.timeout
            )
        except (requests.RequestException, UnicodeEncodeError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return False, None, self.last_error

        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:160]
            self.last_error = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
            return False, None, self.last_error

        try:
            body = response.json()
        except ValueError:
            self.last_error = f"non-JSON response (HTTP {response.status_code})"
            return False, None, self.last_error

        if isinstance(body, dict) and body.get("valid"):
            user = str(body.get("user_name") or "").strip() or None
            self.last_error = None
            return True, user, None

        msg = str((body or {}).get("message") or "Invalid token")
        self.last_error = msg
        return False, None, msg

    def close(self) -> None:
        self._session.close()
