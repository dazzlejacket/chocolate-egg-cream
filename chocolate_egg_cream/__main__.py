"""Chocolate Egg Cream -- command line entry point.

    python -m chocolate_egg_cream             open the window
    python -m chocolate_egg_cream console     run in the terminal instead
    python -m chocolate_egg_cream check       check audio capture & setup readiness
    python -m chocolate_egg_cream verify      verify this build (packaged or source)
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

from . import __version__, credentials, settings as settings_module
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
    from .recognize import create_recognizer

    sample_seconds = float(config["sample_seconds"])
    capture = get_capture(
        macos_backend=str(config.get("macos_capture_backend", "auto") or "auto")
    )
    recognizer = create_recognizer(config)

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
    from .scrobble import lastfm_app
    from .scrobble.lastfm import LastFM

    api_key, api_secret, _custom = lastfm_app.resolve_credentials(config)
    if not api_key or not api_secret:
        print("Last.fm needs an API key and secret (none bundled in this build).", flush=True)
        print(
            "Create an API account at https://www.last.fm/api/account/create and then:\n"
            "  python -m chocolate_egg_cream set lastfm_api_key <key>\n"
            "  python -m chocolate_egg_cream set lastfm_api_secret <secret>",
            flush=True,
        )
        return 1

    lastfm = LastFM(api_key=api_key, api_secret=api_secret)
    try:
        token = lastfm.fetch_token()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not request a Last.fm auth token: {exc}", flush=True)
        return 1

    url = lastfm.auth_url(token)
    print(f"\nOpen this page and click 'Yes, allow access':\n  {url}\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass

    input("Press Enter here once you have allowed the app in your browser... ")

    try:
        session_key, username = lastfm.exchange_token_with_user(token)
    except Exception as exc:  # noqa: BLE001
        print(f"Last.fm refused the token: {exc}", flush=True)
        return 1

    config["lastfm_session_key"] = session_key
    if username:
        config["lastfm_username"] = username
    settings_module.save(config)
    who = f" as @{username}" if username else ""
    print(f"Last.fm is authorised{who}. You can run the app now.\n", flush=True)
    return 0


# -------------------------------------------------------------------- check


def run_check(config: dict) -> int:
    """Verify Python, audio capture helper, GUI, and scrobbler setup."""
    ok = True
    print(f"Python: {sys.version.split()[0]} ({sys.platform})", flush=True)
    print(
        f"Build: {'packaged executable' if getattr(sys, 'frozen', False) else 'source tree'}",
        flush=True,
    )

    from .certs import find_ca_bundle

    bundle = find_ca_bundle()
    if bundle:
        print(f"CA bundle: {bundle}", flush=True)
    else:
        print("CA bundle: MISSING -- HTTPS requests would fail (run 'verify')", flush=True)
        ok = False

    try:
        import tkinter  # noqa: F401
        print("GUI (tkinter): ready", flush=True)
    except ImportError as exc:
        print(f"GUI (tkinter): missing ({exc}) -- console mode will still work", flush=True)

    if sys.platform == "darwin":
        from .capture.macos import helper_path

        try:
            binary = helper_path()
            print(f"macOS audio helper: ready ({binary})", flush=True)
            print(
                "  Note: On macOS 14.2+, Chocolate Egg Cream uses Core Audio Taps "
                "(audio-only permission under System Settings -> Privacy & Security -> "
                "Screen & System Audio Recording -> System Audio Recording Only).",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"macOS audio helper: NOT READY -- {exc}", flush=True)
            ok = False
    else:
        print(f"Audio capture backend: {sys.platform}", flush=True)

    enabled = config.get("enabled_services", [])
    if "lastfm" in enabled:
        lfm_keys = ("lastfm_api_key", "lastfm_api_secret", "lastfm_session_key")
        lfm_missing = [k for k in lfm_keys if not credentials.normalize_secret(config.get(k))]
        lfm_bad = [k for k in lfm_keys if credentials.secret_problem(config.get(k))]
        if lfm_missing:
            print(
                f"Last.fm: needs setup (missing {', '.join(lfm_missing)}; "
                "open Setup in the app)",
                flush=True,
            )
        elif lfm_bad:
            print(
                f"Last.fm: not usable ({', '.join(lfm_bad)} has an invalid value; "
                "re-enter it in Setup)",
                flush=True,
            )
        else:
            print("Last.fm: ready", flush=True)
    if "listenbrainz" in enabled:
        token = config.get("listenbrainz_token")
        if not credentials.normalize_secret(token):
            print("ListenBrainz: needs setup (no token; open Setup in the app)", flush=True)
        elif credentials.secret_problem(token):
            print(
                "ListenBrainz: not usable (token is invalid; re-enter it in Setup)",
                flush=True,
            )
        else:
            print("ListenBrainz: ready", flush=True)

    print(f"Settings file: {settings_module.settings_path()}", flush=True)
    print(f"Log file:      {settings_module.log_path()}", flush=True)
    if not bundle:
        print("\nRun 'verify' for the full packaged-build diagnostics.", flush=True)
    return 0 if ok else 1


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

    def show(kind: str, payload) -> None:
        if kind == "now_playing" and payload is not None:
            print_track(payload)
        elif kind == "scrobbled" and payload is not None:
            print(f"  scrobbled: {payload.label}", flush=True)
        elif kind == "started":
            print("  capturing system audio", flush=True)
        elif kind == "stopped":
            print("  stopped", flush=True)
        elif kind == "error":
            print(f"  error: {payload}", flush=True)

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


def _run_single_instance(run, config: dict, *, want_gui: bool) -> int:
    """Run `run(config)` while holding the single-instance lock.

    Two instances at once would recognise the same music twice, scrobble it
    twice and fight over settings.json.
    """
    import atexit

    from . import instance

    try:
        instance.acquire()
    except instance.AlreadyRunning as exc:
        return _report_already_running(exc, want_gui=want_gui)
    except Exception as exc:  # noqa: BLE001 - a broken lock must not block the app
        print(f"Warning: could not take the instance lock ({exc}); continuing.", flush=True)
    atexit.register(instance.release)
    try:
        return run(config)
    finally:
        instance.release()


def _report_already_running(exc: instance.AlreadyRunning, *, want_gui: bool) -> int:
    if want_gui:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning("Chocolate Egg Cream", str(exc))
            root.destroy()
            return 3
        except Exception:  # noqa: BLE001 - no display: fall back to the console
            pass
    print(str(exc), flush=True)
    return 3


def main() -> int:
    # Windows consoles default to a legacy code page; the em dash in the
    # output would come out as mojibake. In windowed builds stdout/stderr may be None.
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = sys.argv[1:]
    if args and args[0] in ("--version", "-V", "version"):
        print(f"Chocolate Egg Cream {__version__}", flush=True)
        return 0

    config = settings_module.load()
    settings_module.setup_logging(config.get("log_level", "INFO"))

    if not args:
        return _run_single_instance(run_gui, config, want_gui=True)

    command = args[0]
    if command == "verify":
        from . import verify as verify_module

        return verify_module.main(args[1:])
    if command == "console":
        return _run_single_instance(
            lambda cfg: asyncio.run(run_app(cfg)), config, want_gui=False
        )
    if command == "gui":
        return _run_single_instance(run_gui, config, want_gui=True)
    if command == "check":
        return run_check(config)
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
        if key in credentials.SECRET_KEYS:
            problem = credentials.secret_problem(value)
            if problem:
                print(f"{key} was not saved: {problem}", flush=True)
                return 1
            if not value.strip():
                # Empty means "forget this credential" and must actively
                # remove it from the OS credential store.
                settings_module.clear_secret(key)
                config[key] = ""
        if key in ("enabled_services",):
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
        elif key in ("lastfm_use_custom_credentials", "external_scrobbler"):
            config[key] = value.strip().lower() in ("1", "true", "yes", "on")
        elif key == "theme":
            norm = value.strip().lower()
            if norm not in settings_module.KNOWN_THEMES:
                print(f"theme must be one of: {', '.join(settings_module.KNOWN_THEMES)}", flush=True)
                return 2
            config[key] = norm
        else:
            config[key] = value
        settings_module.save(config)
        display_val = "(saved securely)" if key in credentials.SECRET_KEYS else config[key]
        print(f"{key} = {display_val}", flush=True)
        return 0
    if command == "settings":
        print(settings_module.settings_path(), flush=True)
        return 0

    print(__doc__, flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
