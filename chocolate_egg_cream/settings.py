"""Settings and small local state, kept in one JSON file + OS credential store."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import credentials

log = logging.getLogger(__name__)

# Serialises every read/modify/write of settings.json so the GUI thread, the
# engine thread and a second process cannot interleave writes.
_SAVE_LOCK = threading.Lock()

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
    # Recently submitted scrobble events (per service, per playback event).
    # This replaces the old "scrobbled" list, which blocked songs forever.
    "scrobble_events": [],
}

KNOWN_SERVICES = ("lastfm", "listenbrainz")

_INT_SETTINGS = (
    "interval_seconds",
    "sample_seconds",
    "confirmations_required",
    "min_play_seconds",
)
_FLOAT_SETTINGS = ("max_skew",)
_LIST_SETTINGS = ("enabled_services", "scrobble_events", "scrobbled")



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


def _atomic_write(path: Path, text: str) -> None:
    """Write settings in one atomic replace.

    A half-written settings.json used to be possible when two threads (or two
    instances) wrote at the same time, and a crash mid-write left a corrupt
    file behind. Readers now only ever see the old or the new file.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    _restrict_file_permissions(tmp)
    os.replace(tmp, path)
    _restrict_file_permissions(path)


def _quarantine_corrupt(path: Path, exc: Exception) -> None:
    """Move an unreadable settings file aside instead of silently losing it."""
    backup = path.with_name(f"{path.name}.invalid")
    try:
        if backup.exists():
            backup.unlink()
        path.replace(backup)
        log.warning(
            "Settings file %s could not be read (%s: %s); it was moved to %s "
            "and defaults are in use.",
            path,
            type(exc).__name__,
            exc,
            backup,
        )
    except OSError:
        log.warning(
            "Settings file %s could not be read (%s: %s); defaults are in use.",
            path,
            type(exc).__name__,
            exc,
        )


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce what was read from disk back into the types the app expects."""
    checked = dict(DEFAULTS)
    checked.update(data)

    for key in _INT_SETTINGS:
        if key in checked:
            try:
                checked[key] = int(checked[key])
            except (TypeError, ValueError):
                log.warning("Setting %s was %r; using %r", key, checked[key], DEFAULTS[key])
                checked[key] = DEFAULTS[key]
    for key in _FLOAT_SETTINGS:
        if key in checked:
            try:
                checked[key] = float(checked[key])
            except (TypeError, ValueError):
                log.warning("Setting %s was %r; using %r", key, checked[key], DEFAULTS[key])
                checked[key] = DEFAULTS[key]
    for key in _LIST_SETTINGS:
        if key in checked and isinstance(checked[key], str):
            checked[key] = [checked[key]]
        if key in checked and not isinstance(checked[key], list):
            log.warning("Setting %s was not a list; using %r", key, DEFAULTS[key])
            checked[key] = DEFAULTS[key]

    enabled = checked.get("enabled_services") or []
    checked["enabled_services"] = [name for name in enabled if name in KNOWN_SERVICES]

    for key in ("lastfm_api_key", *credentials.SECRET_KEYS):
        checked[key] = str(checked.get(key) or "")

    return checked


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
            else:
                _quarantine_corrupt(path, ValueError("not a JSON object"))
        except (OSError, json.JSONDecodeError) as exc:
            _quarantine_corrupt(path, exc)

    if use_os_keyring:
        for key in credentials.SECRET_KEYS:
            file_value = credentials.normalize_secret(data.get(key))
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

    return validate(data)


def save(data: dict[str, Any], *, use_os_keyring: bool = True) -> None:
    """Write settings to disk, storing secrets in the OS credential store where available."""
    with _SAVE_LOCK:
        _save_locked(data, use_os_keyring=use_os_keyring)


def _save_locked(data: dict[str, Any], *, use_os_keyring: bool) -> None:
    to_write = dict(data)

    if use_os_keyring:
        for key in credentials.SECRET_KEYS:
            value = credentials.normalize_secret(data.get(key))
            if value and credentials.set_secret(key, value):
                # Stored safely in OS credential manager; keep JSON free of
                # plaintext secrets.
                to_write[key] = ""
            elif not value:
                # An empty value never deletes a stored secret: a partial save
                # must not wipe credentials that another call still holds.
                # Clearing is explicit, through clear_secret().
                if credentials.get_secret(key):
                    to_write[key] = ""

    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(to_write, indent=2, ensure_ascii=False))
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)


def clear_secret(key: str) -> None:
    """Explicitly delete a stored secret (used when a credential is emptied)."""
    if key not in credentials.SECRET_KEYS:
        return
    with _SAVE_LOCK:
        credentials.delete_secret(key)



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
