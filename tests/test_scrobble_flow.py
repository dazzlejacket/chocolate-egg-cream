"""Integration test: the real loop against a fake Last.fm API.

Runs the actual recognition loop with a fake capture and a fake
recogniser, and checks what the app sends to Last.fm. Nothing leaves the
machine: the fake API is a local HTTP server.

    python tests/test_scrobble_flow.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from chocolate_egg_cream.app import EggCream
from chocolate_egg_cream.recognize import Recognition

CALLS: list[dict] = []

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILED.append(name)


class FakeLastFM(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        form = parse_qs(raw)
        CALLS.append({key: values[0] for key, values in form.items()})
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeCapture:
    """Returns a loud sample every time."""

    sample_rate = 48000

    def __init__(self):
        self.opened = 0
        self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def read(self, seconds):
        return np.full(int(self.sample_rate * seconds), 0.4, dtype=np.float32)


class FakeRecognizer:
    """Hands out a scripted list of recognitions."""

    def __init__(self, results):
        self.results = list(results)
        self.index = 0

    async def recognize(self, samples, sample_rate):
        item = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        return item

    async def close(self):
        pass


def song(artist: str, title: str, isrc: str) -> Recognition:
    return Recognition(artist=artist, title=title, album=f"{artist} Album", isrc=isrc)


async def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeLastFM)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import chocolate_egg_cream.scrobble.lastfm as lastfm_module

    lastfm_module.API_ROOT = f"http://127.0.0.1:{port}/2.0/"

    a = song("Artist A", "Song A", "AAA111")
    b = song("Artist B", "Song B", "BBB222")

    settings = {
        "lastfm_api_key": "key",
        "lastfm_api_secret": "secret",
        "lastfm_session_key": "session",
        "listenbrainz_token": "",
        "enabled_services": ["lastfm"],
        "interval_seconds": 0,
        "sample_seconds": 0.05,
        "confirmations_required": 2,
        "min_play_seconds": 1,
        "scrobbled": [],
    }

    # A, A confirms A. B, B confirms B and finishes A. A, A confirms A again,
    # but A was already scrobbled so it must not be sent twice.
    recognizer = FakeRecognizer([a, a, b, b, a, a])
    capture = FakeCapture()
    saved = []
    app = EggCream(
        settings,
        capture=capture,
        recognizer=recognizer,
        save_settings=saved.append,  # keep the real settings file untouched
    )

    # Drive the loop by hand with a controlled clock.
    clock = [0.0]

    async def step():
        samples = await asyncio.to_thread(capture.read, 0.05)
        recognition = await recognizer.recognize(samples, capture.sample_rate)
        finished = app.tracks.observe(recognition, clock[0], heard_audio=True)
        if finished is not None:
            await app._finish(finished)
        current = app.tracks.current
        if current is not None and app._now_playing_for != current.identity:
            await app._announce_now_playing()

    for index in range(6):
        clock[0] = float(index) * 10
        await step()

    scrobbles = [call for call in CALLS if call.get("method") == "track.scrobble"]
    now_playing = [call for call in CALLS if call.get("method") == "track.updateNowPlaying"]

    print("what the app sent to Last.fm")
    for call in CALLS:
        method = call.get("method")
        print(f"  {method:22s} {call.get('artist')} - {call.get('track')} ts={call.get('timestamp', '-')}")

    check("song A was scrobbled once", sum(c.get("track") == "Song A" for c in scrobbles) == 1)
    check("song B was scrobbled once", sum(c.get("track") == "Song B" for c in scrobbles) == 1)
    check("no song was scrobbled twice", len(scrobbles) == 2, f"got {len(scrobbles)}")
    check("now playing was sent for each new track", len(now_playing) == 3, f"got {len(now_playing)}")
    check(
        "the scrobble timestamp is when the track started",
        any(c.get("track") == "Song A" and c.get("timestamp") == "10" for c in scrobbles),
        str([c.get("timestamp") for c in scrobbles]),
    )
    check(
        "the album was sent",
        all(c.get("album") for c in scrobbles),
        str([c.get("album") for c in scrobbles]),
    )
    check(
        "the request was signed",
        all(c.get("api_sig") for c in CALLS),
    )
    check(
        "format=json was requested on every Last.fm call",
        all(c.get("format") == "json" for c in CALLS),
        str([c.get("format") for c in CALLS]),
    )
    check(
        "session history recorded plays newest-first",
        [t.title for t in app.history] == ["Song A", "Song B", "Song A"],
        str([t.title for t in app.history]),
    )
    check(
        "the scrobbled songs were remembered",
        set(settings["scrobbled"]) == {"isrc:AAA111", "isrc:BBB222"},
        str(settings["scrobbled"]),
    )
    check(
        "the settings were saved after every scrobble",
        len(saved) == 2,
        f"{len(saved)} saves",
    )

    server.shutdown()
    print()
    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


async def test_without_a_service() -> int:
    """A song must not be marked as scrobbled when nothing was sent."""
    print("\nnothing configured")
    CALLS.clear()
    a = song("Artist A", "Song A", "AAA111")
    b = song("Artist B", "Song B", "BBB222")

    settings = {
        "lastfm_api_key": "",
        "lastfm_api_secret": "",
        "lastfm_session_key": "",
        "listenbrainz_token": "",
        "enabled_services": ["lastfm", "listenbrainz"],
        "interval_seconds": 0,
        "sample_seconds": 0.05,
        "confirmations_required": 2,
        "min_play_seconds": 1,
        "scrobbled": [],
    }

    saved = []
    app = EggCream(
        settings,
        capture=FakeCapture(),
        recognizer=FakeRecognizer([a, a, b, b]),
        save_settings=saved.append,
    )

    clock = [0.0]
    for index in range(4):
        clock[0] = float(index) * 10
        samples = await asyncio.to_thread(app.capture.read, 0.05)
        recognition = await app.recognizer.recognize(samples, 48000)
        finished = app.tracks.observe(recognition, clock[0], heard_audio=True)
        if finished is not None:
            await app._finish(finished)

    check("nothing was sent to Last.fm", not CALLS, str(CALLS))
    check(
        "the song was not marked as scrobbled",
        settings["scrobbled"] == [],
        str(settings["scrobbled"]),
    )
    check("the settings were not saved", not saved, f"{len(saved)} saves")

    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    code = asyncio.run(main())
    code |= asyncio.run(test_without_a_service())
    raise SystemExit(code)
