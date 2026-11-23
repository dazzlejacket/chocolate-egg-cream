"""Tests for the parts that have no network or audio device involved.

    python tests/test_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chocolate_egg_cream.recognize import Recognition, to_wav_bytes
from chocolate_egg_cream.track import TrackState

FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILED.append(name)


def song(artist: str, title: str, isrc: str | None = None) -> Recognition:
    return Recognition(artist=artist, title=title, isrc=isrc)


def test_confirm_twice() -> None:
    print("confirm twice")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")

    check("a single match is not enough", state.observe(a, 0.0) is None)
    check("nothing is current yet", state.current is None)

    state.observe(a, 1.0)
    check("the second match confirms", state.current is not None)
    check("the confirmed track is the right one", state.current.identity == a.identity)


def test_failure_keeps_the_track() -> None:
    print("a failed recognition keeps the current track")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")
    state.observe(a, 0.0)
    state.observe(a, 1.0)

    for at in (2.0, 3.0, 4.0):
        state.observe(None, at)

    check("the track is still current", state.current is not None)
    check("it is still the same track", state.current.identity == a.identity)


def test_song_change() -> None:
    print("a song change is detected")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")
    b = song("Artist B", "Song B")

    state.observe(a, 0.0)
    state.observe(a, 1.0)

    check("one sighting of B changes nothing", state.observe(b, 2.0) is None)
    check("A is still current", state.current.identity == a.identity)

    finished = state.observe(b, 3.0)
    check("the second sighting of B confirms it", finished is not None)
    check("B is now current", state.current.identity == b.identity)
    check("the finished track is A", finished.recognition.identity == a.identity)


def test_playtime() -> None:
    print("playtime")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")
    b = song("Artist B", "Song B")

    state.observe(a, 0.0)
    state.observe(a, 10.0)
    state.observe(a, 20.0)
    check("playtime is counted", abs(state.playtime - 10.0) < 0.001, f"got {state.playtime}")

    state.observe(a, 25.0, heard_audio=False)
    check(
        "silence does not count as playtime",
        abs(state.playtime - 10.0) < 0.001,
        f"got {state.playtime}",
    )

    check("one sighting of B changes nothing", state.observe(b, 30.0) is None)
    finished = state.observe(b, 31.0)
    check("the second sighting of B finishes A", finished is not None)
    if finished is not None:
        # 10s from t=10 to t=20, the silent 20s to 25s is skipped, 5s from
        # t=25 to t=30 and 1s more while B was being confirmed.
        check(
            "the finished track carries its playtime",
            abs(finished.played_seconds - 16.0) < 0.001,
            f"got {finished.played_seconds}",
        )


def test_identity() -> None:
    print("identity")
    one = song("Artist A", "Song A", isrc="AAA")
    two = song("Artist A", "Song A", isrc="AAA")
    three = song("artist a", "song a")
    four = song("Artist A", "Song B")
    five = song("Artist C", "Song D")

    check("the same isrc is the same song", one.identity == two.identity)
    check("no isrc falls back to artist and title", three.identity == "artist a::song a")
    check("the fallback ignores case", three.identity == song("ARTIST A", "SONG A").identity)
    check("different songs are different", four.identity != five.identity)
    check("a song without an isrc is not an isrc song", four.identity != one.identity)


def test_wav_roundtrip() -> None:
    print("wav encoding")
    import numpy as np

    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    data = to_wav_bytes(samples, 48000)
    check("the wav starts with a RIFF header", data[:4] == b"RIFF")
    check("the wav has the right length", len(data) == 44 + 10)


def main() -> int:
    for test in (
        test_confirm_twice,
        test_failure_keeps_the_track,
        test_song_change,
        test_playtime,
        test_identity,
        test_wav_roundtrip,
    ):
        test()
    print()
    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
