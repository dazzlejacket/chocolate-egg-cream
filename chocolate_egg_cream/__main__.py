"""Chocolate Egg Cream -- command line entry point.

    python -m chocolate_egg_cream             open the window
    python -m chocolate_egg_cream console     run in the terminal instead
    python -m chocolate_egg_cream test        take one sample and recognise it
    python -m chocolate_egg_cream auth lastfm run the Last.fm authorisation
    python -m chocolate_egg_cream set <key> <value>   change one setting
    python -m chocolate_egg_cream settings    show where the settings live
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import webbrowser

import numpy as np

from . import settings as settings_module
from .app import EggCream, is_silent
from .recognize import Recognition


def format_time(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}:{rest:02d}"


def print_track(track: Recognition, playtime: float | None = None) -> None:
    extra = f" ({format_time(playtime)})" if playtime is not None else ""
    print(f"  now playing: {track.label}{extra}", flush=True)


# --------------------------------------------------------------------- test


async def run_test(config: dict) -> int:
    """Capture one sample and print what Shazam makes of it."""
    from .capture import get_capture
    from .recognize import ShazamRecognizer

    sample_seconds = float(config["sample_seconds"])
    capture = get_capture()
    recognizer = ShazamRecognizer(segment_seconds=int(sample_seconds))

    print(f"Capturing {sample_seconds:.0f}s of system audio ...", flush=True)
    started = time.time()
    capture.open()
    try:
        samples = await asyncio.to_thread(capture.read, sample_seconds)
    finally:
        capture.close()

    print(f"Captured {samples.size} samples in {time.time() - started:.1f}s", flush=True)

    loudness = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
    print(f"RMS {loudness:.5f}" + ("  (silent)" if is_silent(samples) else ""), flush=True)

    if is_silent(samples):
        print("Nothing was playing, so there was nothing to recognise.", flush=True)
        return 2

    started = time.time()
    track = await recognizer.recognize(samples, capture.sample_rate)
    took = time.time() - started

    await recognizer.close()
    if track is None:
        print("No match.", flush=True)
        return 1
    print(f"\n{track.artist} — {track.title}")
    print(f"  recognised in {took:.2f}s", flush=True)
    return 0


# --------------------------------------------------------------------- auth


def run_lastfm_auth(config: dict) -> int:
    """Walk the user through the Last.fm authorisation."""
    from .scrobble.lastfm import LastFM

    api_key = config.get("lastfm_api_key", "")
    api_secret = config.get("lastfm_api_secret", "")
    if not api_key or not api_secret:
        print("Last.fm needs an API key and secret.", flush=True)
        print(
            "Create an API account at https://www.last.fm/api/account/create and then:\n"
            "  python -m chocolate_egg_cream set lastfm_api_key <key>\n"
            "  python -m chocolate_egg_cream set lastfm_api_secret <secret>",
            flush=True,
        )
        return 1

    lastfm = LastFM(api_key=api_key, api_secret=api_secret)
    url = lastfm.auth_url()
    print(f"\nOpen this page and allow the app:\n  {url}\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass

    token = input("Paste the token Last.fm shows you: ").strip()
    if not token:
        print("No token given, giving up.", flush=True)
        return 1

    try:
        session_key = lastfm.exchange_token(token)
    except Exception as exc:  # noqa: BLE001
        print(f"Last.fm refused the token: {exc}", flush=True)
        return 1

    config["lastfm_session_key"] = session_key
    settings_module.save(config)
    print("Last.fm is authorised. You can run the app now.\n", flush=True)
    return 0


# --------------------------------------------------------------------- gui


def run_gui(config: dict) -> int:
    """Open the small window, falling back to the console if it cannot."""
    try:
        from .gui import run as run_window
    except ImportError as exc:
        print(f"The window needs tkinter, which is not available ({exc}).", flush=True)
        print("Falling back to the console.", flush=True)
        return asyncio.run(run_app(config))
    return run_window(config)


# ---------------------------------------------------------------------- run


async def run_app(config: dict) -> int:
    """The tiny console app."""
    app = EggCream(config)
    commands: asyncio.Queue = asyncio.Queue()

    def show(kind: str, track: Recognition | None) -> None:
        if kind == "now_playing" and track is not None:
            print_track(track)
        elif kind == "started":
            print("  capturing system audio", flush=True)
        elif kind == "stopped":
            print("  stopped", flush=True)

    app.on_event = show

    def read_commands() -> None:
        while True:
            try:
                line = input()
            except EOFError:
                loop.call_soon_threadsafe(commands.put_nowait, "quit")
                return
            # Piped input can start with a byte order mark.
            command = line.strip().lower().lstrip("\ufeff")
            loop.call_soon_threadsafe(commands.put_nowait, command)

    print(
        "\nChocolate Egg Cream\n"
        "  start | stop | status | quit\n",
        flush=True,
    )

    loop = asyncio.get_running_loop()
    await app.start()
    threading.Thread(target=read_commands, daemon=True).start()

    while True:
        command = await commands.get()
        if command in ("quit", "exit"):
            break
        if command == "start":
            if app.running:
                print("  already running", flush=True)
            else:
                await app.start()
        elif command == "stop":
            if app.running:
                await app.stop()
            else:
                print("  not running", flush=True)
        elif command == "status":
            if app.running:
                track = app.current
                if track is None:
                    print("  running, nothing recognised yet", flush=True)
                else:
                    print_track(track, app.playtime)
            else:
                print("  stopped", flush=True)
        elif command:
            print("  unknown command", flush=True)

    await app.shutdown()
    print("  bye", flush=True)
    return 0


# --------------------------------------------------------------------- main


def main() -> int:
    # Windows consoles default to a legacy code page; the em dash in the
    # output would come out as mojibake.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    config = settings_module.load()
    settings_module.setup_logging(config.get("log_level", "INFO"))

    args = sys.argv[1:]
    if not args:
        return run_gui(config)

    command = args[0]
    if command == "console":
        return asyncio.run(run_app(config))
    if command == "gui":
        return run_gui(config)
    if command == "test":
        return asyncio.run(run_test(config))
    if command == "auth":
        if len(args) > 1 and args[1] == "lastfm":
            return run_lastfm_auth(config)
        print("Usage: python -m chocolate_egg_cream auth lastfm", flush=True)
        return 2
    if command == "set":
        if len(args) < 3:
            print("Usage: python -m chocolate_egg_cream set <key> <value>", flush=True)
            return 2
        key, value = args[1], " ".join(args[2:])
        if key in ("enabled_services", "scrobbled"):
            config[key] = [item.strip() for item in value.split(",") if item.strip()]
        elif key == "max_skew":
            config[key] = float(value)
        elif key in (
            "interval_seconds",
            "sample_seconds",
            "min_play_seconds",
            "confirmations_required",
        ):
            config[key] = int(value)
        else:
            config[key] = value
        settings_module.save(config)
        print(f"{key} = {config[key]}", flush=True)
        return 0
    if command == "settings":
        print(settings_module.settings_path(), flush=True)
        return 0

    print(__doc__, flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
