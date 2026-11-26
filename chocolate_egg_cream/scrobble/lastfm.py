"""Last.fm scrobbling.

Uses the classic Last.fm API with an API key, an API secret and a session
key. The desktop authorisation flow calls `auth.getToken`, opens the
approval page in the browser with that token, and then exchanges it via
`auth.getSession` once the user clicks Allow.

Stale or revoked sessions (error codes 4, 9, 14) are detected cleanly and
surfaced with the actual Last.fm API error so the user can reconnect easily.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse

import requests

from .. import __version__, credentials

log = logging.getLogger(__name__)

API_ROOT = "https://ws.audioscrobbler.com/2.0/"
AUTH_ROOT = "https://www.last.fm/api/auth/"

# Never let a request hang the shutdown path: Stop/Quit must stay responsive.
DEFAULT_TIMEOUT = 10.0

# Last.fm API error codes that indicate the session key is invalid/revoked
# and the user needs to re-authorise their account.
#   4  - Authentication Failed
#   9  - Invalid session key - Please re-authenticate
#   14 - This token has not been authorized
STALE_SESSION_ERRORS = frozenset({4, 9, 14})


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


def _parse_json_error(response: requests.Response) -> tuple[int | None, str | None, dict | None]:
    """Extract Last.fm JSON error (code, message, body) even on HTTP 4xx."""
    try:
        body = response.json()
    except ValueError:
        return None, None, None
    if isinstance(body, dict) and "error" in body:
        try:
            code = int(body.get("error"))
        except (TypeError, ValueError):
            code = None
        msg = str(body.get("message") or "Last.fm API error")
        return code, msg, body
    return None, None, body if isinstance(body, dict) else None


class LastFM:
    """Scrobbles to Last.fm."""

    name = "Last.fm"
    service_key = "lastfm"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        session_key: str = "",
        username: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = credentials.normalize_secret(api_key)
        self.api_secret = credentials.normalize_secret(api_secret)
        self.session_key = credentials.normalize_secret(session_key)
        self.username = str(username or "").strip()
        self.timeout = timeout
        self.last_error: str | None = None
        self.last_error_code: int | None = None
        self.session_stale: bool = False
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
        self._session.headers.update({"User-Agent": f"ChocolateEggCream/{__version__}"})

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
        code, msg, body = _parse_json_error(response)
        if code is not None:
            raise RuntimeError(f"Last.fm error {code}: {msg}")
        response.raise_for_status()
        token = (body or {}).get("token")
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

    def exchange_token_with_user(self, token: str) -> tuple[str, str]:
        """Trade the approved token for `(session_key, username)`."""
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
        code, msg, body = _parse_json_error(response)
        if code is not None:
            raise RuntimeError(f"Last.fm error {code}: {msg}")
        response.raise_for_status()
        session = (body or {}).get("session") or {}
        key = str(session.get("key") or "").strip()
        user = str(session.get("name") or "").strip()
        if not key:
            raise RuntimeError("Last.fm did not return a session key")
        self.session_key = key
        self.username = user
        self._problems["session key"] = credentials.secret_problem(self.session_key)
        self.session_stale = False
        self.last_error = None
        self.last_error_code = None
        return key, user

    def exchange_token(self, token: str) -> str:
        """Trade the approved token for a permanent session key."""
        key, _user = self.exchange_token_with_user(token)
        return key

    def verify_session(self) -> tuple[bool, str | None, str | None]:
        """Verify that the stored session key is still valid and fetch username.

        Returns `(ok, username, error_message)`. Sets `self.session_stale` when
        Last.fm reports an invalid/revoked session key.
        """
        if not self.configured:
            err = self.problem or "not configured"
            self.last_error = err
            return False, None, err

        params = {
            "method": "user.getInfo",
            "api_key": self.api_key,
            "sk": self.session_key,
        }
        params["api_sig"] = sign(params, self.api_secret)
        params["format"] = "json"
        try:
            response = self._session.get(API_ROOT, params=params, timeout=self.timeout)
        except (requests.RequestException, UnicodeEncodeError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.last_error_code = None
            return False, None, self.last_error

        code, msg, body = _parse_json_error(response)
        if code is not None:
            self.last_error_code = code
            self.last_error = f"error {code}: {msg}"
            if code in STALE_SESSION_ERRORS:
                self.session_stale = True
            return False, None, self.last_error

        if response.status_code >= 400:
            self.last_error_code = None
            self.last_error = f"HTTP {response.status_code}"
            return False, None, self.last_error

        user_obj = (body or {}).get("user") or {}
        name = str(user_obj.get("name") or "").strip()
        if name:
            self.username = name
        self.session_stale = False
        self.last_error = None
        self.last_error_code = None
        return True, self.username or None, None

    def now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
        isrc: str | None = None,
    ) -> bool:
        """Tell Last.fm what is playing right now."""
        del isrc  # Last.fm API does not take ISRC
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
        isrc: str | None = None,
    ) -> bool:
        """Submit a finished play."""
        del isrc
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
            self.last_error_code = None
            log.warning("Last.fm call skipped: %s", self.last_error)
            return False
        params["api_sig"] = sign(params, self.api_secret)
        params["format"] = "json"
        try:
            # Last.fm write methods (track.scrobble, track.updateNowPlaying) require POST.
            response = self._session.post(API_ROOT, data=params, timeout=self.timeout)
        except (requests.RequestException, UnicodeEncodeError, ValueError) as exc:
            # ValueError/UnicodeEncodeError cover malformed header/field values,
            # which must not escape into the recognition loop.
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.last_error_code = None
            log.warning("Last.fm request failed: %s", self.last_error)
            return False

        # Parse Last.fm JSON error BEFORE checking HTTP status: Last.fm returns
        # HTTP 403 with JSON {"error": 9, "message": "Invalid session key..."}.
        code, msg, body = _parse_json_error(response)
        if code is not None:
            self.last_error_code = code
            self.last_error = f"error {code}: {msg}"
            if code in STALE_SESSION_ERRORS:
                self.session_stale = True
            log.warning("Last.fm refused the call: %s", self.last_error)
            return False

        if response.status_code >= 400:
            self.last_error_code = None
            detail = " ".join(response.text.split())[:160]
            self.last_error = f"HTTP {response.status_code}" + (f": {detail}" if detail else "")
            log.warning("Last.fm HTTP failure: %s", self.last_error)
            return False

        if body is None:
            self.last_error_code = None
            self.last_error = f"non-JSON response (HTTP {response.status_code})"
            log.warning("Last.fm returned a non-JSON response (HTTP %s)", response.status_code)
            return False

        # For track.scrobble, Last.fm can return HTTP 200 with ignored=1.
        if params.get("method") == "track.scrobble" and isinstance(body.get("scrobbles"), dict):
            scrobbles_obj = body["scrobbles"]
            attr = scrobbles_obj.get("@attr") or {}
            try:
                accepted = int(attr.get("accepted", 1))
                ignored = int(attr.get("ignored", 0))
            except (TypeError, ValueError):
                accepted, ignored = 1, 0
            if accepted == 0 and ignored > 0:
                item = scrobbles_obj.get("scrobble")
                if isinstance(item, list) and item:
                    item = item[0]
                ign = (item or {}).get("ignoredMessage") if isinstance(item, dict) else {}
                ign_code = (ign or {}).get("code", "")
                ign_text = (ign or {}).get("#text", "") or "Scrobble ignored by Last.fm"
                self.last_error_code = None
                self.last_error = f"ignored ({ign_code}): {ign_text}" if ign_code else ign_text
                log.warning("Last.fm ignored scrobble: %s", self.last_error)
                return False

        self.last_error = None
        self.last_error_code = None
        self.session_stale = False
        return True

    def close(self) -> None:
        self._session.close()
