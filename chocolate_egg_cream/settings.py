"""Settings and small local state, kept in one JSON file."""

from __future__ import annotations

import json
import logging
import os
import warnings
from pathlib import Path
from typing import Any

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


def load() -> dict[str, Any]:
    """Read the settings, falling back to the defaults."""
    data = dict(DEFAULTS)
    path = settings_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update(stored)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s: %s", path, exc)
    return data


def save(data: dict[str, Any]) -> None:
    """Write the settings back to disk."""
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)


def setup_logging(level: str = "INFO") -> None:
    """Log to the console and to a small local file."""
    # Loopback capture always reports a discontinuity when it starts, which
    # is expected and only noise in the console.
    warnings.filterwarnings("ignore", message=".*data discontinuity in recording.*")

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        file_handler = None

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if file_handler is not None:
        handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
