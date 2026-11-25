"""Last.fm scrobbling.

Uses the classic Last.fm API with an API key, an API secret and a session
key. The desktop authorisation flow calls `auth.getToken`, opens the
approval page in the browser with that token, and then exchanges it via
`auth.getSession` once the user clicks Allow.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse

import requests

from .. import credentials

log = logging.getLogger(__name__)

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
AUTH_ROOT = "https://www.last.fm/api/auth/"

# Never let a request hang the shutdown path: Stop/Quit must stay responsive.
DEFAULT_TIMEOUT = 10.0


def sign(params: dict[str, str], secret: str) -> str:
    """The Last.fm API signature: md5 over the sorted parameters.

    Per Last.fm's API spec, `format` and `callback` must be excluded from
    the signature.
    """
    joined = "".join(
        f"{key}{params[key]}"
        for key in sorted(params)
        if key not in ("format", "callback")
    )
    return hashlib.md5((joined + secret).encode("utf-8")).hexdigest()


class LastFM:
    """Scrobbles to Last.fm."""

    name = "Last.fm"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        session_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = credentials.normalize_secret(api_key)
        self.api_secret = credentials.normalize_secret(api_secret)
        self.session_key = credentials.normalize_secret(session_key)
        self.timeout = timeout
        self.last_error: str | None = None
        self._problems = {
            "API key": credentials.secret_problem(self.api_key),
            "API secret": credentials.secret_problem(self.api_secret),
            "session key": credentials.secret_problem(self.session_key),
        }
        for label, problem in self._problems.items():
            if problem:
                # The reason only: never the value.
                log.warning(
                    "Last.fm %s is not usable (%s). Re-enter it under Setup… -> Last.fm.",
                    label,
                    problem,
                )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "ChocolateEggCream/1.0"})

    @property
    def configured(self) -> bool:
        if not (self.api_key and self.api_secret and self.session_key):
            return False
        return all(problem is None for problem in self._problems.values())

    @property
    def problem(self) -> str | None:
        """Why Last.fm cannot be used, or None when it is ready."""
        missing = [
            label
            for label, value in (
                ("API key", self.api_key),
                ("API secret", self.api_secret),
                ("session key", self.session_key),
            )
            if not value
        ]
        if missing:
            return "missing " + ", ".join(missing)
        for label, problem in self._problems.items():
            if problem:
                return f"{label} {problem}"
        return None

    def fetch_token(self) -> str:
        """Request a temporary auth token for the desktop authorisation flow."""
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Last.fm needs an API key and an API secret before authorising.")
        problem = credentials.secret_problem(self.api_key) or credentials.secret_problem(
            self.api_secret
        )
        if problem:
            raise RuntimeError(f"The Last.fm credentials cannot be used: {problem}")
        params = {
            "method": "auth.getToken",
            "api_key": self.api_key,
        }
        params["api_sig"] = sign(params, self.api_secret)
        params["format"] = "json"
        response = self._session.get(API_ROOT, params=params, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(body.get("message", "Last.fm refused the token request"))
        token = body.get("token")
        if not token:
            raise RuntimeError("Last.fm did not return an authorisation token")
        return str(token)

    def auth_url(self, token: str | None = None) -> str:
        """The URL the user opens to authorise the app."""
        query_params = {"api_key": self.api_key}
        if token:
            query_params["token"] = token
        query = urllib.parse.urlencode(query_params)
        return f"{AUTH_ROOT}?{query}"

    def exchange_token(self, token: str) -> str:
        """Trade the approved token for a permanent session key."""
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Last.fm needs an API key and an API secret before authorising.")
        params = {
            "method": "auth.getSession",
            "api_key": self.api_key,
            "token": token,
        }
        params["api_sig"] = sign(params, self.api_secret)
        params["format"] = "json"
        response = self._session.get(API_ROOT, params=params, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(body.get("message", "Last.fm refused the token"))
        return body["session"]["key"]

    def now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
    ) -> bool:
        """Tell Last.fm what is playing right now."""
        params: dict[str, str] = {
            "method": "track.updateNowPlaying",
            "api_key": self.api_key,
            "sk": self.session_key,
            "artist": artist,
            "track": title,
        }
        if album:
            params["album"] = album
        if duration:
            params["duration"] = str(int(duration))
        return self._call(params)

    def scrobble(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        timestamp: int | None = None,
        duration: float | None = None,
    ) -> bool:
        """Submit a finished play."""
        params: dict[str, str] = {
            "method": "track.scrobble",
            "api_key": self.api_key,
            "sk": self.session_key,
            "artist": artist,
            "track": title,
            "timestamp": str(int(timestamp if timestamp is not None else time.time())),
        }
        if album:
            params["album"] = album
        if duration:
            params["duration"] = str(int(duration))
        return self._call(params)

    def _call(self, params: dict[str, str]) -> bool:
        if not self.configured:
            self.last_error = self.problem or "not configured"
            log.warning("Last.fm call skipped: %s", self.last_error)
            return False
        params["api_sig"] = sign(params, self.api_secret)
        params["format"] = "json"
        try:
            # Last.fm write methods (track.scrobble, track.updateNowPlaying) require POST.
            response = self._session.post(API_ROOT, data=params, timeout=self.timeout)
            response.raise_for_status()
        except (requests.RequestException, UnicodeEncodeError, ValueError) as exc:
            # ValueError/UnicodeEncodeError cover malformed header/field values,
            # which must not escape into the recognition loop.
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("Last.fm request failed: %s", self.last_error)
            return False

        try:
            body = response.json()
        except ValueError:
            self.last_error = f"non-JSON response (HTTP {response.status_code})"
            log.warning("Last.fm returned a non-JSON response (HTTP %s)", response.status_code)
            return False

        if "error" in body:
            self.last_error = f"error {body.get('error')}: {body.get('message')}"
            log.warning("Last.fm refused the call: %s", self.last_error)
            return False
        self.last_error = None
        return True

    def close(self) -> None:
        self._session.close()
