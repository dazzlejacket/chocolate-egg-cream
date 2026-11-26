"""Dedicated Chocolate Egg Cream Last.fm application credential loader.

Private application credentials are NEVER committed to the repository.
During a release or local build, set:

    CEC_LASTFM_API_KEY
    CEC_LASTFM_API_SECRET

and the build script writes a gitignored `chocolate_egg_cream/_bundled_lastfm.py`
module that PyInstaller bundles into the packaged app. Users then only need to
click Authorize in Last.fm — no API account creation required.

Custom user-supplied credentials remain available under Advanced in Setup.
"""

from __future__ import annotations

import os

from .. import credentials


def _load_injected() -> tuple[str, str]:
    """Load build-time injected Last.fm app credentials, if present."""
    try:
        from .. import _bundled_lastfm  # type: ignore[attr-defined]

        key = credentials.normalize_secret(getattr(_bundled_lastfm, "API_KEY", ""))
        secret = credentials.normalize_secret(getattr(_bundled_lastfm, "API_SECRET", ""))
        if key and secret:
            return key, secret
    except ImportError:
        pass
    return "", ""


def bundled_credentials() -> tuple[str, str]:
    """Return `(api_key, api_secret)` for the bundled Chocolate Egg Cream app.

    Runtime environment variables override build-time injected credentials
    (handy for local development without writing files).
    """
    env_key = credentials.normalize_secret(os.environ.get("CEC_LASTFM_API_KEY", ""))
    env_secret = credentials.normalize_secret(os.environ.get("CEC_LASTFM_API_SECRET", ""))
    if env_key and env_secret:
        return env_key, env_secret
    return _load_injected()


def has_bundled_credentials() -> bool:
    """True when dedicated Chocolate Egg Cream Last.fm app credentials are bundled."""
    key, secret = bundled_credentials()
    return bool(key and secret)


def resolve_credentials(settings: dict) -> tuple[str, str, bool]:
    """Return `(api_key, api_secret, using_custom)` for the current settings.

    Priority:
    1. If `lastfm_use_custom_credentials` is explicitly True, use custom keys.
    2. If bundled app credentials are available, use them — UNLESS this is a
       legacy v1.0.x config (no `lastfm_session_mode` recorded yet) that already
       has custom keys + a session key. Once the user authorizes with bundled
       credentials (`lastfm_session_mode == "bundled"`), bundled credentials are
       always used even if old custom keys remain in settings.
    3. Otherwise fall back to custom keys from settings.
    """
    custom_key = credentials.normalize_secret(settings.get("lastfm_api_key", ""))
    custom_secret = credentials.normalize_secret(settings.get("lastfm_api_secret", ""))
    want_custom = bool(settings.get("lastfm_use_custom_credentials", False))
    session_mode = str(settings.get("lastfm_session_mode", "") or "").strip().lower()

    if want_custom:
        return custom_key, custom_secret, True

    b_key, b_secret = bundled_credentials()
    if b_key and b_secret:
        if session_mode == "bundled":
            return b_key, b_secret, False
        # Preserve seamless upgrade for v1.0.x users who already authorized
        # with their own API key + secret + session key.
        if (
            session_mode in ("", "custom")
            and custom_key
            and custom_secret
            and credentials.normalize_secret(settings.get("lastfm_session_key", ""))
        ):
            return custom_key, custom_secret, True
        return b_key, b_secret, False

    return custom_key, custom_secret, True
