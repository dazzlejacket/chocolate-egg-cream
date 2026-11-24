"""Settings and small local state, kept in one JSON file + OS credential store."""

from __future__ import annotations

import json
import logging
import os
import sys
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import credentials

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # Last.fm
    "lastfm_api_key": "",
    "lastfm_api_secret": "",
    "lastfm_session_key": "",
    # ListenBrainz
    "listenbrainz_token": "",
    # Which services to submit to: any of "lastfm" and "listenbrainz".
    "enabled_services": ["lastfm", "listenbrainz"],
    # Recognition provider ("shazam" by default).
    "recognition_provider": "shazam",
    # macOS capture backend: "auto" (Core Audio Taps on 14.2+, SCK on 13),
    # "coreaudio", or "screencapturekit".
    "macos_capture_backend": "auto",
    # How often to take a sample, and how long each sample is.
    "interval_seconds": 25,
    "sample_seconds": 12,
    # How many matching recognitions are needed to confirm a track.
    "confirmations_required": 2,
    # Reject a match whose time or frequency skew is larger than this.
    # 0 turns the check off.
    "max_skew": 0,
    # A track with no known duration has to play this long before it counts.
    "min_play_seconds": 120,
    # Songs already scrobbled, so the same song is never sent twice.
    "scrobbled": [],
}


def config_dir() -> Path:
    """Where the app keeps its settings."""
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "chocolate-egg-cream"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def log_path() -> Path:
    return config_dir() / "eggcream.log"


def _restrict_file_permissions(path: Path) -> None:
    """Ensure the settings file is owner-readable only on POSIX systems."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def load(*, use_os_keyring: bool = True) -> dict[str, Any]:
    """Read the settings, loading secrets from the OS credential store when available."""
    data = dict(DEFAULTS)
    path = settings_path()
    plaintext_migrated = False

    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update(stored)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s: %s", path, exc)

    if use_os_keyring:
        for key in credentials.SECRET_KEYS:
            file_value = str(data.get(key) or "")
            stored_secret = credentials.get_secret(key)
            if stored_secret:
                data[key] = stored_secret
                if file_value:
                    plaintext_migrated = True
            elif file_value:
                # Migrate legacy plaintext secret from settings.json into OS store.
                if credentials.set_secret(key, file_value):
                    plaintext_migrated = True

        if plaintext_migrated:
            save(data, use_os_keyring=True)

    return data


def save(data: dict[str, Any], *, use_os_keyring: bool = True) -> None:
    """Write settings to disk, storing secrets in the OS credential store where available."""
    to_write = dict(data)

    if use_os_keyring:
        for key in credentials.SECRET_KEYS:
            secret_value = str(data.get(key) or "")
            if credentials.set_secret(key, secret_value):
                # Stored safely in OS credential manager; keep JSON free of plaintext secrets.
                to_write[key] = ""

    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_write, indent=2, ensure_ascii=False), encoding="utf-8")
        _restrict_file_permissions(path)
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)


def setup_logging(level: str = "INFO") -> None:
    """Log to a bounded local file (and stderr when a console is attached)."""
    # Loopback capture always reports a discontinuity when it starts, which
    # is expected and only noise in the console.
    warnings.filterwarnings("ignore", message=".*data discontinuity in recording.*")

    path = log_path()
    file_handler: logging.Handler | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
    except OSError:
        file_handler = None

    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if file_handler is not None:
        handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
        force=True,
    )
